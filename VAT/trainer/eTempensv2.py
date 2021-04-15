#!coding:utf-8
import torch
from torch.nn import functional as F

import os
import datetime
from pathlib import Path
from itertools import cycle
from collections import defaultdict

from utils.loss import mse_with_softmax
from utils.ramps import exp_rampup
from utils.datasets import decode_label
from utils.data_utils import NO_LABEL

class Trainer:

    def __init__(self, model, optimizer, device, config):
        print('Tempens-v2 with epoch pseudo labels')
        self.model     = model
        self.optimizer = optimizer
        self.ce_loss   = torch.nn.CrossEntropyLoss(ignore_index=NO_LABEL)
        self.mse_loss  = mse_with_softmax # F.mse_loss 
        self.save_dir  = '{}-{}_{}-{}_{}'.format(config.arch, config.model,
                          config.dataset, config.num_labels,
                          datetime.datetime.now().strftime("%Y-%m-%d-%H-%M"))
        self.save_dir  = os.path.join(config.save_dir, self.save_dir)
        self.device      = device
        self.usp_weight  = config.usp_weight
        self.ema_decay   = config.ema_decay
        self.rampup      = exp_rampup(config.rampup_length)
        self.save_freq   = config.save_freq
        self.print_freq  = config.print_freq
        self.epoch       = 0
        self.start_epoch = 0
                
    def train_iteration(self, label_loader, unlab_loader, print_freq):
        loop_info = defaultdict(list)
        batch_idx, label_n, unlab_n = 0, 0, 0
        for (label_x, label_y, ldx), (unlab_x, unlab_y, udx) in zip(cycle(label_loader), unlab_loader):
            label_x, label_y = label_x.to(self.device), label_y.to(self.device)
            unlab_x, unlab_y = unlab_x.to(self.device), unlab_y.to(self.device)
            ##=== decode targets of unlabeled data ===
            self.decode_targets(unlab_y)
            lbs, ubs = label_x.size(0), unlab_x.size(0)

            ##=== forward ===
            outputs = self.model(label_x)
            loss = self.ce_loss(outputs, label_y)
            loop_info['lSup'].append(loss.item())

            ##=== Semi-supervised Training Phase ===
            unlab_outputs    = self.model(unlab_x)
            iter_unlab_pslab = self.epoch_pslab[udx]
            uloss  = self.mse_loss(unlab_outputs, iter_unlab_pslab)
            uloss *= self.rampup(self.epoch)*self.usp_weight
            loss  += uloss; loop_info['uTmp'].append(uloss.item())
            ## update pseudo labels
            with torch.no_grad():
                self.epoch_pslab[udx] = unlab_outputs.clone().detach()
            ## bachward 
            self.optimizer.zero_grad()
            loss.backward()
            self.optimizer.step()

            ##=== log info ===
            batch_idx, label_n, unlab_n = batch_idx+1, label_n+lbs, unlab_n+ubs
            loop_info['lacc'].append(label_y.eq(outputs.max(1)[1]).float().sum().item())
            loop_info['uacc'].append(unlab_y.eq(unlab_outputs.max(1)[1]).float().sum().item())
            if print_freq>0 and (batch_idx%print_freq)==0:
                print(f"[train][{batch_idx:<3}]", self.gen_info(loop_info, lbs, ubs))
        # temporal ensemble
        self.update_ema_predictions() # update every epoch
        print(">>>[train]", self.gen_info(loop_info, label_n, unlab_n, False))
        return loop_info, label_n

    def test_iteration(self, data_loader, print_freq):
        loop_info = defaultdict(list)
        label_n, unlab_n = 0, 0
        for batch_idx, (data, targets) in enumerate(data_loader):
            data, targets = data.to(self.device), targets.to(self.device)
            ##=== decode targets ===
            lbs, ubs = data.size(0), -1

            ##=== forward ===
            outputs = self.model(data)
            loss = self.ce_loss(outputs, targets)
            loop_info['lloss'].append(loss.item())

            ##=== log info ===
            label_n, unlab_n = label_n+lbs, unlab_n+ubs
            loop_info['lacc'].append(targets.eq(outputs.max(1)[1]).float().sum().item())
            if print_freq>0 and (batch_idx%print_freq)==0:
                print(f"[test][{batch_idx:<3}]", self.gen_info(loop_info, lbs, ubs))
        print(">>>[test]", self.gen_info(loop_info, label_n, unlab_n, False))
        return loop_info, label_n

    def train(self, label_loader, unlab_loader, print_freq=20):
        self.model.train()
        with torch.enable_grad():
            return self.train_iteration(label_loader, unlab_loader, print_freq)

    def test(self, data_loader, print_freq=10):
        self.model.eval()
        with torch.no_grad():
            return self.test_iteration(data_loader, print_freq)

    def update_ema_predictions(self):
        """update every epoch"""
        self.ema_pslab = (self.ema_decay*self.ema_pslab) + (1.0-self.ema_decay)*self.epoch_pslab
        self.epoch_pslab = self.ema_pslab / (1.0 - self.ema_decay**((self.epoch-self.start_epoch)+1.0))

    def loop(self, epochs, label_data, unlab_data, test_data, scheduler=None):
        ## construct epoch pseudo labels
        self.epoch_pslab = self.create_soft_pslab(n_samples=len(unlab_data.dataset),
                                           n_classes=unlab_data.dataset.num_classes,
                                                                       dtype='rand')
        self.ema_pslab   = self.create_soft_pslab(n_samples=len(unlab_data.dataset),
                                           n_classes=unlab_data.dataset.num_classes,
                                                                       dtype='zero')
        ## main process
        best_info, best_acc, n = None, 0., 0
        for ep in range(epochs):
            self.epoch = ep
            if scheduler is not None: scheduler.step()
            print("------ Training epochs: {} ------".format(ep))
            self.train(label_data, unlab_data, self.print_freq)
            print("------ Testing epochs: {} ------".format(ep))
            info, n = self.test(test_data, self.print_freq)
            acc     = sum(info['lacc']) / n
            if acc>best_acc: best_info, best_acc = info, acc
            ## save model
            if self.save_freq!=0 and (ep+1)%self.save_freq == 0:
                self.save(ep)
        print(f">>>[best]", self.gen_info(best_info, n, n, False))

    def create_soft_pslab(self, n_samples, n_classes, dtype='rand'):
        if dtype=='rand': 
             pslab = torch.randint(0, n_classes, (n_samples,n_classes))
        elif dtype=='zero':
             pslab = torch.zeros(n_samples, n_classes)
        else:
             raise ValueError('Unknown pslab dtype: {}'.format(dtype))
        return pslab.to(self.device)

    def decode_targets(self, targets):
        label_mask = targets.ge(0)
        unlab_mask = targets.le(NO_LABEL)
        targets[unlab_mask] = decode_label(targets[unlab_mask])
        return label_mask, unlab_mask

    def gen_info(self, info, lbs, ubs, iteration=True):
        ret = []
        nums = {'l': lbs, 'u':ubs, 'a': lbs+ubs}
        for k, val in info.items():
            n = nums[k[0]]
            v = val[-1] if iteration else sum(val)
            s = f'{k}: {v/n:.3%}' if k[-1]=='c' else f'{k}: {v:.5f}'
            ret.append(s)
        return '\t'.join(ret)

    def save(self, epoch, **kwargs):
        if self.save_dir is not None:
            model_out_path = Path(self.save_dir)
            state = {"epoch": epoch,
                    "weight": self.model.state_dict()}
            if not model_out_path.exists():
                model_out_path.mkdir()
            save_target = model_out_path / "model_epoch_{}.pth".format(epoch)
            torch.save(state, save_target)
            print('==> save model to {}'.format(save_target))
