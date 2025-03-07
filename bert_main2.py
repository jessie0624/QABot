import argparse
from collections import Counter
import code
import os
import logging
import random
from tqdm import tqdm, trange

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.utils.data import DataLoader, RandomSampler, SequentialSampler, TensorDataset

from transformers import AdamW, WarmupLinearSchedule
from transformers import BertConfig, BertForSequenceClassification, BertTokenizer
from transformers import glue_convert_examples_to_features as convert_examples_to_features
from transformers.data.processors.utils import DataProcessor, InputExample

import numpy as np
import pandas as pd

from scipy.stats import pearsonr, spearmanr
from sklearn.metrics import matthews_corrcoef, f1_score

logger = logging.getLogger(__name__)

def set_seed(args):
    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)

def simple_accuracy(preds, labels):
    return (preds == labels).mean()

def acc_and_f1(preds, labels):
    acc = simple_accuracy(preds, labels)
    f1 = f1_score(y_true=labels, y_pred=preds)
    return {
        "acc": acc,
        "f1": f1,
        "acc_and_f1": (acc + f1) / 2,
    }

def pearson_and_spearman(preds, labels):
    pearson_corr = pearsonr(preds, labels)[0]
    spearman_corr = spearmanr(preds, labels)[0]
    return {
        "pearson": pearson_corr,
        "spearmanr": spearman_corr,
        "corr": (pearson_corr + spearman_corr) / 2,
    }

def acc_f1_pea_spea(preds, labels):
    acc_f1 = acc_and_f1(preds, labels)
    pea_spea = pearson_and_spearman(preds,labels)
    return {**acc_f1, **pea_spea}

class FAQProcessor(DataProcessor):
    
    def get_train_examples(self, data_dir):
        return self._create_examples(os.path.join(data_dir, 'train.csv'))
    
    def get_dev_examples(self, data_dir):
        return self._create_examples(os.path.join(data_dir, 'dev.csv'))

    def get_labels(self):
        return [0, 1]

    def _create_examples(self, path):
        df = pd.read_csv(path)
        examples = []
        titles = [str(t) for t in df['title'].tolist()]
        replies = [str(t) for t in df['reply'].tolist()]
        labels = df['is_best'].astype('int').tolist()
        for i in range(len(labels)):
            examples.append(
                InputExample(guid=i, text_a=titles[i], text_b=replies[i], label=labels[i]))
        return examples
# 训练模型
def train(args, train_dataset, model, optimizer, scheduler, device, tokenizer, loss_file, acc_file):
    model.train()
    train_sampler = RandomSampler(train_dataset)
    train_dataloader = DataLoader(train_dataset, sampler=train_sampler, batch_size=args.train_batch_size)

    tr_loss = 0.0
    logging_loss = 0.0
    global_step = 0
    preds = None
    out_label_ids = None
    epoch_iterator = tqdm(train_dataloader, desc='Iteration')
    for step, batch in enumerate(epoch_iterator):
        batch_preds = None
        batch = tuple(t.to(args.device) for t in batch)
        inputs = {'input_ids': batch[0], 'attention_mask': batch[1], 'token_type_ids': batch[2], 'labels': batch[3]}
        
        outputs = model(**inputs)
        loss, logits = outputs[:2]
        
        
        #if args.gradient_accumulation_steps > 1:
            #loss = loss / args.gradient_accumulation_steps
        loss.backward()
        torch.nn.utils.clip_grad_norm_(model.parameters(), args.max_grad_norm)
        tr_loss += loss.item()
        logging_loss += loss.item()

        #if (step + 1) % args.gradient_accumulation_steps == 0:
        optimizer.step() 
        scheduler.step() # update learning rate schedule
        model.zero_grad()
        global_step += 1       

        batch_preds = logits.detach().cpu().numpy() 
        batch_out_label_ids = inputs['labels'].detach().cpu().numpy()
        if preds is None:
            preds = batch_preds
        else:
            preds = np.append(preds, batch_preds, axis=0)
        
        if out_label_ids is None:
            out_label_ids = batch_out_label_ids
        else:
            out_label_ids = np.append(out_label_ids, inputs['labels'].detach().cpu().numpy(), axis=0)
        
        #print('loss value: {}, loss per batch:{}'.format(loss, loss/args.train_batch_size))
        
        if global_step % 100 == 0:
            #total_loss = tr_loss / (global_step * args.train_batch_size)
            # print("iteration: {}, loss: {}".format(global_step, total_loss))
            with open(loss_file, 'a+') as writer:
                writer.write("iteration: {}, lr: {}, loss: {}\n".format(global_step, scheduler.get_lr()[0], logging_loss/(global_step*args.train_batch_size)))
            logging_loss = 0.0
            total_preds = np.argmax(preds, axis=1)
            results = acc_f1_pea_spea(total_preds, out_label_ids)
            # code.interact(local=locals())
            with open(acc_file, 'a+') as acc_writer:
                acc_writer.write('iteration: {}, lr: {}, loss: {}, results:{}\n'.format(global_step, scheduler.get_lr()[0], logging_loss/(global_step*args.train_batch_size), results))
            print(results)
            print('\n')
            preds = None
            out_label_ids=None
    print('\n total loss:{}\n'.format(tr_loss/global_step))
            
def evaluate(args, eval_dataset, model, device, tokenizer):
    model.eval()
    eval_sampler = RandomSampler(eval_dataset)
    eval_dataloader = DataLoader(eval_dataset, sampler=eval_sampler, batch_size=args.train_batch_size)

    tr_loss = 0.0
    global_step = 0
    preds = None
    out_label_ids = None
    epoch_iterator = tqdm(eval_dataloader, desc='Iteration')
    with torch.no_grad():
        for step, batch in enumerate(epoch_iterator):
            batch = tuple(t.to(args.device) for t in batch)
            inputs = {'input_ids': batch[0], 'attention_mask': batch[1], 'token_type_ids': batch[2], 'labels': batch[3]}
            # if step == 0:
            #     print(inputs)
            outputs = model(**inputs)
            loss, logits = outputs[:2]
            batch_preds = None
            if args.gradient_accumulation_steps > 1:
                loss = loss / args.gradient_accumulation_steps
            tr_loss += loss.item()    
            global_step += 1

            batch_preds = logits.detach().cpu().numpy() 
            batch_out_label_ids = inputs['labels'].detach().cpu().numpy()
            if preds is None:
                preds = batch_preds
            else:
                preds = np.append(preds, batch_preds, axis=0)
            if out_label_ids is None:
                out_label_ids = batch_out_label_ids
            else:
                out_label_ids = np.append(out_label_ids, inputs['labels'].detach().cpu().numpy(), axis=0)
            
        total_loss = tr_loss / (global_step * args.train_batch_size)
        print("iteration: {}, loss: {}".format(global_step, total_loss))
        preds = np.argmax(preds, axis=1)
        results = acc_f1_pea_spea(preds, out_label_ids)
        print(total_loss, results)
    return (total_loss,results)

def load_and_cache_examples(args, tokenizer, evaluate=False):
    processor = FAQProcessor()
    cached_features_file = "cached_{}_bert".format("dev" if evaluate else 'train')
    if os.path.exists(cached_features_file):
        features = torch.load(cached_features_file)
    else:
        label_list = processor.get_labels()
        examples = processor.get_dev_examples(args.data_dir) if evaluate else processor.get_train_examples(args.data_dir)
        # print(len(examples))
        features = convert_examples_to_features(
                                examples=examples,
                                tokenizer=tokenizer,
                                max_length=args.max_seq_length,
                                label_list=label_list,
                                output_mode='classification',
                                pad_on_left=False,
                                pad_token=tokenizer.convert_tokens_to_ids([tokenizer.pad_token])[0],
                                pad_token_segment_id=0)
        logger.info('saving features into cached file %s', cached_features_file)
        torch.save(features, cached_features_file)
    
    '''
        InputExample:
            self.guid = guid
            self.text_a = text_a
            self.text_b = text_b
            self.label = label
        InputFeatures:
            self.input_ids = input_ids
            self.attention_mask = attention_mask
            self.token_type_ids = token_type_ids
            self.label = label

            features.append(
                InputFeatures(input_ids=input_ids,
                              attention_mask=attention_mask,
                              token_type_ids=token_type_ids,
                              label=label))
    '''
    ## convert tensors and build dataset
    all_input_ids = torch.tensor([f.input_ids for f in features],dtype=torch.long)
    all_attention_mask = torch.tensor([f.attention_mask for f in features], dtype=torch.long)
    all_token_type_ids = torch.tensor([f.token_type_ids for f in features], dtype=torch.long)
    all_label = torch.tensor([f.label for f in features], dtype=torch.long)
    dataset = TensorDataset(all_input_ids, all_attention_mask, all_token_type_ids, all_label)
    return dataset

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--seed', type=int, default=42,
                        help="random seed for initialization")
    parser.add_argument("--data_dir", default=None, type=str, required=True,
                        help="directory containing the data")
    parser.add_argument("--output_dir", default="BERT_output", type=str, required=True,
                        help="The model output save dir")
    parser.add_argument("--do_train", action='store_true', help="Whether to run training.")
    parser.add_argument("--do_eval", action='store_true', help="Whether to run eval on the dev set.")
    parser.add_argument("--evaluate_during_training", action='store_true',
                        help="Run evaluation during training at each logging step.")

    parser.add_argument("--max_seq_length", default=100, type=int, required=False, 
                        help="maximum sequence length for BERT sequence classificatio")
    parser.add_argument("--max_steps", default=-1, type=int,
                        help="If > 0: set total number of training steps to perform. Override num_train_epochs.")
    parser.add_argument("--warmup_steps", default=0, type=int,
                        help="Linear warmup over warmup_steps.")
    parser.add_argument('--gradient_accumulation_steps', type=int, default=1,
                        help="Number of updates steps to accumulate before performing a backward/update pass.")
    parser.add_argument("--num_train_epochs", default=3, type=int,
                        help="Total number of training epochs to perform.")
    parser.add_argument("--learning_rate", default=1e-5, type=float,
                        help="The initial learning rate for Adam.")
    parser.add_argument("--weight_decay", default=0.0, type=float,
                        help="Weight deay if we apply some.")
    parser.add_argument("--max_grad_norm", default=1.0, type=float,
                        help="Max gradient norm.")
    parser.add_argument("--adam_epsilon", default=1e-8, type=float,
                        help="Epsilon for Adam optimizer.")

    parser.add_argument("--train_batch_size", default=64, type=int, required=False,
                        help="batch size for train and eval")
    parser.add_argument('--logging_steps', type=int, default=50,
                        help="Log every X updates steps.")
    parser.add_argument('--log_path', default=None, type=str, required=False)

    args = parser.parse_args()
    logging.basicConfig(format = '%(asctime)s - %(levelname)s - %(name)s -   %(message)s',
                        datefmt = '%m/%d/%Y %H:%M:%S',
                        level = logging.INFO)
    set_seed(args)
    ## get train and dev data
    print('loading dataset...')
    processor = FAQProcessor()
    label_list = processor.get_labels()
    num_labels = len(label_list)
    config = BertConfig.from_pretrained('bert-base-chinese', cache_dir='./cache_down', num_labels=num_labels)
    tokenizer = BertTokenizer.from_pretrained('bert-base-chinese', cache_dir='./cache_down')

    train_dataset = load_and_cache_examples(args, tokenizer, evaluate=False)
    eval_dataset = load_and_cache_examples(args, tokenizer, evaluate=True)

    ## 构建模型
    model =  BertForSequenceClassification.from_pretrained("./cache_down/pytorch_model.bin", config=config)
    args.device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    model = model.to(args.device)
    # print(model)

    ## 损失函数
    no_decay = ['bias', 'LayerNorm.weight']
    optimizer_grouped_parameters = [
        {'params': [p for n, p in model.named_parameters() if not any(nd in n for nd in no_decay)],
         'weight_decay': args.weight_decay},
        {'params': [p for n, p in model.named_parameters() if any(nd in n for nd in no_decay)],
         'weight_decay': 0.0}
    ]
    optimizer = AdamW(optimizer_grouped_parameters, lr=args.learning_rate, eps=args.adam_epsilon)
    t_total = len(train_dataset) // args.gradient_accumulation_steps * args.num_train_epochs * args.train_batch_size
    scheduler = WarmupLinearSchedule(optimizer, warmup_steps=args.warmup_steps, t_total=t_total)

    ## training 
    logger.info('*****Running training*******')
    logger.info(' Num examples = %d', len(train_dataset))
    logger.info(' Gradient Accumulation steps = %d', args.gradient_accumulation_steps)
    
    best_acc_f1 = 0
    if not os.path.exists(os.path.join(args.output_dir, args.log_path)):
        os.makedirs(os.path.join(args.output_dir, args.log_path))
    else:
        for file in os.listdir(os.path.join(args.output_dir, args.log_path)):
            os.remove(os.path.join(args.output_dir, args.log_path, file))
    train_loss_file = os.path.join(args.output_dir, args.log_path, 'train_loss_file.txt')
    train_acc_file = os.path.join(args.output_dir, args.log_path, 'train_acc_file.txt')
    eval_loss_file = os.path.join(args.output_dir, args.log_path, 'eval_loss_file.txt')
    for epoch in range(args.num_train_epochs):
        logger.info(' Num epochs = %d', epoch)
        train(args, train_dataset, model, optimizer, scheduler, args.device, tokenizer,train_loss_file,train_acc_file)
        results = evaluate(args, eval_dataset, model, args.device, tokenizer)
        with open(eval_loss_file, 'a+') as eval_writer:
            eval_writer.write('epoch:{}, lr: {}, eval_loss:{}, result: {}\n'.format(epoch, scheduler.get_lr()[0],results[0], results[1]))
        if results[1]['acc_and_f1'] > best_acc_f1:
            best_acc_f1 = results[1]['acc_and_f1']
            print('saving best model')
            model_to_save = model.module if hasattr(model, 'module') else model
            model_to_save.save_pretrained(os.path.join(args.output_dir, args.log_path))
            tokenizer.save_pretrained(os.path.join(args.output_dir, args.log_path))
            torch.save(args, os.path.join(args.output_dir, args.log_path, 'training_args_bert.bin'))


if __name__== "__main__":
    main()
