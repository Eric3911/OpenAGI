# -*- coding:utf-8 -*-
# @author: 木子川
# @Data:   2022/8/1
# @Email:  m21z50c71@163.com

import os
import time

import torch
import torch.nn as nn
from torch.utils.data import DataLoader
from tqdm import tqdm
from utils import read_data, built_curpus, TextDataset
from model import TextCNNModel
from config import parsers
import pickle as pkl
from sklearn.metrics import accuracy_score
import logging
from log import logger_init
import time
from test import test_data


if __name__ == "__main__":
    start = time.time()
    args = parsers()
    logger_init(log_level=logging.INFO)
    train_text, train_label = read_data(args.train_file)
    dev_text, dev_label = read_data(args.dev_file)

    device = "cuda:0" if torch.cuda.is_available() else "cpu"

    if os.path.exists(args.data_pkl):
        dataset = pkl.load(open(args.data_pkl, "rb"))
        word_2_index, words_embedding = dataset[0], dataset[1]
    else:
        word_2_index, words_embedding = built_curpus(train_text, args.embedding_num)

    train_dataset = TextDataset(train_text, train_label, word_2_index, args.max_len)
    train_loader = DataLoader(train_dataset, args.batch_size, shuffle=True)

    dev_dataset = TextDataset(dev_text, dev_label, word_2_index, args.max_len)
    dev_loader = DataLoader(dev_dataset, args.batch_size, shuffle=False)

    model = TextCNNModel(words_embedding, args.max_len, args.class_num, args.num_filters).to(device)
    opt = torch.optim.AdamW(model.parameters(), lr=args.learn_rate)
    loss_fn = nn.CrossEntropyLoss()

    acc_max = float("-inf")
    for epoch in range(args.epochs):
        model.train()
        loss_sum, count = 0, 0
        for batch_index, (batch_text, batch_label) in enumerate(train_loader):
            batch_text, batch_label = batch_text.to(device), batch_label.to(device)
            pred = model(batch_text)

            loss = loss_fn(pred, batch_label)
            opt.zero_grad()
            loss.backward()
            opt.step()

            loss_sum += loss
            count += 1

            # 打印内容
            if len(train_loader) - batch_index <= len(train_loader) % 1000 and count == len(train_loader) % 1000:
                msg = "[{0}/{1:5d}]\tTrain_Loss:{2:.4f}"
                logging.info(msg.format(epoch + 1, batch_index + 1, loss_sum / count))
                loss_sum, count = 0.0, 0

            if batch_index % 1000 == 999:
                msg = "[{0}/{1:5d}]\tTrain_Loss:{2:.4f}"
                logging.info(msg.format(epoch + 1, batch_index + 1, loss_sum / count))
                loss_sum, count = 0.0, 0

        model.eval()
        all_pred, all_true = [], []
        with torch.no_grad():
            for batch_text, batch_label in tqdm(dev_loader):
                batch_text = batch_text.to(device)
                batch_label = batch_label.to(device)
                pred = model(batch_text)

                pred = torch.argmax(pred, dim=1)
                pred = pred.cpu().numpy().tolist()
                label = batch_label.cpu().numpy().tolist()

                all_pred.extend(pred)
                all_true.extend(label)

        acc = accuracy_score(all_pred, all_true)
        logging.info(f"dev acc:{acc:.4f}")

        if acc > acc_max:
            acc_max = acc
            torch.save(model.state_dict(), args.save_model_best)
            logging.info(f"以保存最佳模型")

    torch.save(model.state_dict(), args.save_model_last)

    end = time.time()
    logging.info(f"运行时间：{(end-start)/60%60:.4f} min")
    test_data()
