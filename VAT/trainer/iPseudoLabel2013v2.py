#!coding:utf-8
import torch
from torch.nn import functional as F

import os
import datetime
from pathlib import Path
from itertools import cycle
from collections import defaultdict

from utils.datasets import decode_label
from utils.data_utils import NO_LABEL
from utils.ramps import exp_rampup, pseudo_rampup

class Trainer:

    def __init__(self, model, optimizer, device, config):
        print('Pseudo-Label-v2 2013 with iteration pseudo labels')
        self.model     = model
        self.optimizer = optimizer
        self.ce_loss   = torch.nn.CrossEntropyLoss(ignore_index=NO_LABEL)
        self.save_dir  = '{}-{}_{}-{}_{}'.format(config.arch, config.model,
                          config.dataset, config.num_labels,
                          datetime.datetime.now().strftime("%Y-%m-%d-%H-%M"))
        self.save_dir  = os.path.join(config.save_dir, self.save_dir)
        self.usp_weight  = config.usp_weight
        #self.rampup      = pseudo_rampup(config.t1, config.t2)
        self.rampup      = exp_rampup(config.weight_rampup)
        self.save_freq   = config.save_freq
        self.print_freq  = config.print_freq
        self.device      = device
        self.epoch       = 0
        
    def train_iteration(self, label_loader, unlab_loader, print_freq):
        loop_info = defaultdict(list)
        batch_idx, label_n, unlab_n = 0, 0, 0
        for (label_x, label_y), (unlab_x, unlab_y) in zip(cycle(label_loader), unlab_loader):
            label_x, label_y = label_x.to(self.device), label_y.to(self.device)
            unlab_x, unlab_y = unlab_x.to(self.device), unlab_y.to(self.device)
            ##=== decode targets of unlabeled data ===
            self.decode_targets(unlab_y)
            lbs, ubs = label_x.size(0), unlab_x.size(0)

            ##=== forward ===
            outputs = self.model(label_x)
            loss = self.ce_loss(outputs, label_y)
            loop_info['lloss'].append(loss.item())

            ##=== Semi-supervised Training ===
            ## pslab loss
            unlab_outputs = self.model(unlab_x)
            with torch.no_grad():
                iter_unlab_pslab = unlab_outputs.max(1)[1]
                iter_unlab_pslab.detach_()
            uloss  = self.ce_loss(unlab_outputs, iter_unlab_pslab)
            uloss *= self.rampup(self.epoch)*self.usp_weight
            loss  += uloss; loop_info['uloss'].append(uloss.item())
            ## backward
            self.optimizer.zero_grad()
            loss.backward()
            self.optimizer.step()

            ##=== log info ===
            batch_idx, label_n, unlab_n = batch_idx+1, label_n+lbs, unlab_n+ubs
            loop_info['lacc'].append(label_y.eq(outputs.max(1)[1]).float().sum().item())
            loop_info['uacc'].append(unlab_y.eq(unlab_outputs.max(1)[1]).float().sum().item())
            if print_freq>0 and (batch_idx%print_freq)==0:
                print(f"[train][{batch_idx:<3}]", self.gen_info(loop_info, lbs, ubs))
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

    def loop(self, epochs, label_data, unlab_data, test_data, scheduler=None):
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
