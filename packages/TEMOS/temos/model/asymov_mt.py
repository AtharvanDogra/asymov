from fnmatch import translate
from typing import List, Iterable, Optional, Dict, Union
import math
import pdb
import sys
from tqdm import tqdm

from omegaconf import DictConfig, ListConfig
from hydra.utils import instantiate

import torch
from torch import Tensor, nn
from torchmetrics import MetricCollection, Accuracy, BLEUScore

from temos.model.metrics.compute_asymov import Perplexity, ReconsMetrics
from torchmetrics import MetricCollection, Accuracy, BLEUScore, SumMetric
from temos.model.base import BaseModel
from temos.model.utils.tools import create_mask, remove_padding, greedy_decode

class AsymovMT(BaseModel):
    def __init__(self,
                 transformer: DictConfig,
                 losses: DictConfig,
                 metrics: DictConfig,
                 optim: DictConfig,
                #  text_vocab_size: int,
                 mw_vocab_size: int,
                 special_symbols: Union[List[str],ListConfig],
                #  fps: float,
                 max_frames: int,
                 metrics_start_epoch: int,
                 metrics_every_n_epoch: int,
                 **kwargs):
        super().__init__()

        self.PAD_IDX, self.BOS_IDX, self.EOS_IDX, self.UNK_IDX = \
            special_symbols.index('<pad>'), special_symbols.index('<bos>'), special_symbols.index('<eos>'), special_symbols.index('<unk>')
        self.num_special_symbols = len(special_symbols)
        
        # self.fps = fps
        self.max_frames = max_frames
        
        self.metrics_start_epoch = metrics_start_epoch
        self.metrics_every_n_epoch = metrics_every_n_epoch
        
        self.transformer = instantiate(transformer)#, src_vocab_size = text_vocab_size, tgt_vocab_size = mw_vocab_size)
        for p in self.transformer.parameters():
            if p.dim() > 1:
                nn.init.xavier_uniform_(p)
        # pdb.set_trace()

        self.optimizer = instantiate(optim, params=self.parameters())

        self._losses = MetricCollection({split: instantiate(losses, vae=False, _recursive_=False)
                                         for split in ["losses_train", "losses_val"]})
        self.losses = {key: self._losses["losses_" + key] for key in ["train", "val"]}

        self.train_metrics = {
                              'acc_teachforce': Accuracy(num_classes=mw_vocab_size, mdmc_average='samplewise',
                                              ignore_index=self.PAD_IDX, multiclass=True,# subset_accuracy=True
                                              ),
                              'bleu_teachforce': BLEUScore(),
                              'ppl_teachforce': Perplexity(self.PAD_IDX),
                             }
        self.val_metrics = {
                              'acc_teachforce': Accuracy(num_classes=mw_vocab_size, mdmc_average='samplewise',
                                              ignore_index=self.PAD_IDX, multiclass=True,# subset_accuracy=True
                                              ),
                              'bleu_teachforce': BLEUScore(),
                              'ppl_teachforce': Perplexity(self.PAD_IDX),
                            
                            #   'acc': Accuracy(num_classes=mw_vocab_size, mdmc_average='samplewise',
                            #                   ignore_index=self.PAD_IDX, multiclass=True,# subset_accuracy=True
                            #                   ),
                              'bleu': BLEUScore(),
                            #   'ppl': Perplexity(self.PAD_IDX),
                              'mpjpe': instantiate(metrics)
                             }

        self.metrics={key: getattr(self, f"{key}_metrics") for key in ["train", "val"]}
        
        self.__post_init__()
        
    #TODO: optimize, and beam search
    def translate(self, src_list: List[Tensor], max_len: Union[int, List[int]]) -> List[Tensor]: # no teacher forcing
        if type(max_len)==int:
            max_len_list = [max_len]*len(src_list)
        else:
            assert len(src_list)==len(max_len)
            max_len_list = max_len
        
        tgt_list = []
        # pdb.set_trace()
        for src, max_len in tqdm(zip(src_list, max_len_list), "translating", len(src_list), None, position=0):
            src = src.view(-1,1) #[Frames, 1]
            tgt_tokens = greedy_decode(self.transformer, src, max_len, self.BOS_IDX, self.EOS_IDX).flatten() #[Frames]
            tgt_list.append(tgt_tokens) 
        return tgt_list # List[Tensor[Frames]]
    
    def allsplit_step(self, split: str, batch: Dict, batch_idx):
        src: Tensor = batch["text"] #[Frames, Batch size]
        tgt: Tensor = batch["motion_words"] #[Frames, Batch size]
        tgt_input = tgt[:-1, :] #[Frames-1, Batch size]
        tgt_out = tgt[1:, :].permute(1,0) #[Batch size, Frames-1]

        src_mask, tgt_mask, src_padding_mask, tgt_padding_mask = create_mask(src, tgt_input, self.PAD_IDX)
        logits = self.transformer(src, tgt_input, src_mask, tgt_mask,src_padding_mask, tgt_padding_mask, src_padding_mask)
        logits = logits.permute(1,2,0) #[Batch size, Classes, Frames]
        
        # Compute the losses
        loss = self.losses[split].update(ds_text=logits, ds_ref=tgt_out)

        if loss is None:
            raise ValueError("Loss is None, this happend with torchmetrics > 0.7")
        # pdb.set_trace()

        ### Compute the metrics
        probs = logits.detach().softmax(dim=1) 
        # bs, _, frames = probs.shape
        target = tgt_out.detach()

        self.metrics[split]['acc_teachforce'].update(probs, target)

        # predicted through teacher forcing, target motion word ids without padding for BLEU
        pred_mw_tokens_teachforce = remove_padding(torch.argmax(probs, dim=1).int(), batch["mw_length"])
        pred_mw_sents_teachforce = [" ".join(map(str, mw.int().tolist())) for mw in pred_mw_tokens_teachforce]
        target_mw_sents = [[" ".join(map(str, mw.int().tolist()))] for mw in remove_padding(target, batch["mw_length"])]
        self.metrics[split]['bleu_teachforce'].update(pred_mw_sents_teachforce, target_mw_sents)
        
        self.metrics[split]['ppl_teachforce'].update(logits.detach().cpu(), target.cpu())

        epoch = self.trainer.current_epoch
        if split == "val" and (epoch==0 or (epoch>=self.metrics_start_epoch and epoch%self.metrics_every_n_epoch==0)):
            # inferencing translations without teacher forcing
            #TODO: add max_len buffer frames to config
            # max_len = [i+int(self.fps*5) for i in batch["mw_length"]]
            # pdb.set_trace()
            pred_mw_tokens = self.translate(remove_padding(src.permute(1,0), batch["text_length"]), self.max_frames) 
            pred_mw_sents = [" ".join(map(str, mw.int().tolist())) for mw in pred_mw_tokens]
            self.metrics[split]['bleu'].update(pred_mw_sents, target_mw_sents)
            
            # Remove terminal tokens BOS/EOS, shift for special symbols
            pred_mw_clusters = [(mw[1:-1] if mw[-1]==self.EOS_IDX else mw[1:]) - self.num_special_symbols for mw in pred_mw_tokens]
            self.metrics[split]['mpjpe'].update(batch['keyid'], pred_mw_clusters)

        return loss

    def allsplit_epoch_end(self, split: str, outputs):
        # pdb.set_trace()
        losses = self.losses[split]
        loss_dict = losses.compute(split)
        dico = {losses.loss2logname(loss, split): value.item()
                for loss, value in loss_dict.items()}

        #Accuracy, BLEU and Perplexity Teacher-forced
        metrics_dict = {f"Metrics/{name}/{split}": metric.compute() for name, metric in self.metrics[split].items() if name.endswith('_teachforce')}
        dico.update(metrics_dict)

        epoch = self.trainer.current_epoch
        if split == "val" and (epoch==0 or (epoch>=self.metrics_start_epoch and epoch%self.metrics_every_n_epoch==0)):
            # pdb.set_trace()
            dico.update({f"Metrics/bleu/{split}": self.metrics[split]['bleu'].compute()})
            mpjpe_dict = self.metrics[split]['mpjpe'].compute()
            dico.update({f"ReconsMetrics/{name}/{split}": metric for name, metric in mpjpe_dict.items()})
            
        dico.update({"epoch": float(self.trainer.current_epoch),
                    "step": float(self.trainer.global_step)})
        self.log_dict(dico)
