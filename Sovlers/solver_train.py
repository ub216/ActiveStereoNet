import torch
import os
import time
import torch.nn as nn
import torch.optim as optim
import torch.nn.functional as F
from Data import get_loader
from Models import get_model
from Losses import get_losses
from Metrics.metrics import epe_metric
from Metrics.metrics import tripe_metric
import pdb

class TrainSolver(object):

    def __init__(self, config):

        self.config = config
        self.cfg_solver = config['solver']
        self.cfg_dataset = config['data']
        self.cfg_model = config['model']
        self.reloaded = True if self.cfg_solver['resume_iter'] > 0 else False

        self.max_disp = self.cfg_model['max_disp']
        self.loss_name = self.cfg_model['loss']
        self.train_loader, self.val_loader = get_loader(self.config)
        self.model = get_model(self.config)

        self.crit = get_losses(self.loss_name, max_disp=self.max_disp)

        if self.cfg_solver['optimizer_type'].lower() == 'rmsprop':
            self.optimizer = optim.RMSprop(self.model.parameters(), lr=self.cfg_solver['lr_init'])
        elif self.cfg_solver['optimizer_type'].lower() == 'adam':
            self.optimizer = optim.Adam(self.model.parameters(), lr=self.cfg_solver['lr_init'])
        else:
            raise NotImplementedError('Optimizer type [{:s}] is not supported'.format(self.cfg_solver['optimizer_type']))
        self.scheduler = optim.lr_scheduler.MultiStepLR(self.optimizer, milestones=self.cfg_solver['milestones'], gamma=self.cfg_solver['gamma'])
        self.global_step = 1

    def save_checkpoint(self):

        ckpt_root = os.path.join(self.cfg_solver['exp_prefix'], self.cfg_solver['model_name'], 'models')
        
        if not os.path.exists(ckpt_root):
            os.makedirs(ckpt_root)
        
        ckpt_name = 'iter_{:d}.pth'.format(self.global_step)
        states = {
            'global_step': self.global_step,
            'model_state': self.model.state_dict(),
            'optimizer_state': self.optimizer.state_dict(),
            'scheduler_state': self.scheduler.state_dict()
        }
        ckpt_full = os.path.join(ckpt_root, ckpt_name)
        
        torch.save(states, ckpt_full)
    
    def load_checkpoint(self):

        ckpt_root = os.path.join(self.cfg_solver['exp_prefix'], self.cfg_solver['model_name'], 'models')

        ckpt_name = 'iter_{:d}.pth'.format(self.cfg_solver['resume_iter'])
        
        ckpt_full = os.path.join(ckpt_root, ckpt_name)

        states = torch.load(ckpt_full, map_location=lambda storage, loc: storage)

        self.global_step = states['global_step']
        self.model.load_state_dict(states['model_state'])
        self.optimizer.load_state_dict(states['optimizer_state'])
        self.scheduler.load_state_dict(states['scheduler_state'])

    def run(self):
        
        self.model.cuda()
        
        print('Number of model parameters: {}'.format(sum([p.data.nelement() for p in self.model.parameters()])))

        if self.cfg_solver['resume_iter'] > 0:
            self.load_checkpoint()
            print('[{:d}] Model loaded.'.format(self.global_step))
        
        data_iter = iter(self.train_loader)
        while True:
            try:
                data_batch = data_iter.next()
            except StopIteration:
                data_iter = iter(self.train_loader)
                data_batch = data_iter.next()

            if self.global_step > self.cfg_solver['max_steps']:
                break

            start_time = time.time()
            
            self.model.train()
            imgL, imgR, disp_L, _ = data_batch
            imgL, imgR, disp_L = imgL.cuda(), imgR.cuda(), disp_L.cuda()
            
            self.optimizer.zero_grad()

            disp_pred = self.model(imgL, imgR)

            #pdb.set_trace()
            
            #auxLoss = 0.2 * self.crit(aux_pred_1, disp_L)# + 0.4 * self.crit(aux_pred_2, disp_L) + 0.6 * self.crit(aux_pred_3, disp_L)
            RHloss = self.crit(disp_pred, disp_L)

            loss = RHloss #+ auxLoss
            loss.backward()
            self.optimizer.step()
            
            elapsed = time.time() - start_time
            train_EPE = epe_metric(disp_L, disp_pred, self.max_disp)
            train_3PE = tripe_metric(disp_L, disp_pred, self.max_disp)
            
            print(
                '[{:d}/{:d}] Train Loss = {:.6f}, EPE = {:.3f} px, 3PE = {:.3f}% time = {:.3f}s.'.format(
                    self.global_step, self.cfg_solver['max_steps'],
                    loss.item(),
                    train_EPE, train_3PE * 100,
                    elapsed
                ), end='\r'
            )

            self.scheduler.step()
            
            if self.global_step % self.cfg_solver['eval_steps'] == 0 and not self.reloaded:
                start_time = time.time()
                self.model.eval()
                with torch.no_grad():
                    
                    val_EPE_metric = 0.0
                    val_TriPE_metric = 0.0
                    N_total = 0.0
                    
                    for val_batch in self.val_loader:
                        imgL, imgR, disp_L, _ = val_batch
                        imgL, imgR, disp_L = imgL.cuda(), imgR.cuda(), disp_L.cuda()

                        N_curr = imgL.shape[0]
                        
                        disp_pred = self.model(imgL, imgR)
                        
                        val_EPE_metric += epe_metric(disp_L, disp_pred, self.max_disp) * N_curr 
                        val_TriPE_metric += tripe_metric(disp_L, disp_pred, self.max_disp) * N_curr

                        N_total += N_curr
                    
                    val_EPE_metric /= N_total
                    val_TriPE_metric /= N_total

                    elapsed = time.time() - start_time
                    print('')
                    print(
                        '[{:d}/{:d}] Validation: EPE = {:.6f} px, 3PE = {:.3f} %, time = {:.3f} s.'.format(
                            self.global_step, self.cfg_solver['max_steps'],
                            val_EPE_metric, val_TriPE_metric * 100, elapsed / N_total
                        )
                    )
            


            if self.global_step % self.cfg_solver['save_steps'] == 0 and not self.reloaded:
                self.save_checkpoint()
                print('')
                print('[{:d}] Model saved.'.format(self.global_step))
                print('')
                
            self.global_step += 1

            self.reloaded = False