# -*- coding:utf-8 -*-
# @author: 木子川
# @Data:   2022/8/8
# @Email:  m21z50c71@163.com

import torch
from tqdm import tqdm
from utils import read_data, TextDataset
from config import parsers
from torch.utils.data import DataLoader
from model import TextCNNModel
from sklearn.metrics import accuracy_score, f1_score, recall_score, precision_score
import logging
from log import logger_init
import pickle as pkl


def test_data():
    args = parsers()
    logger_init(log_level=logging.INFO)
    device = "cuda:0" if torch.cuda.is_available() else "cpu"

    dataset = pkl.load(open(args.data_pkl, "rb"))
    word_2_index, words_embedding = dataset[0], dataset[1]

    test_text, test_label = read_data(args.test_file)
    test_dataset = TextDataset(test_text, test_label, word_2_index, args.max_len)
    test_dataloader = DataLoader(test_dataset, batch_size=args.batch_size, shuffle=False)

    model = TextCNNModel(words_embedding, args.max_len, args.class_num, args.num_filters).to(device)
    model.load_state_dict(torch.load(args.save_model_best))
    model.eval()

    all_pred, all_true = [], []
    with torch.no_grad():
        for batch_text, batch_label in tqdm(test_dataloader):
            batch_label, batch_label = batch_label.to(device), batch_label.to(device)
            pred = model(batch_text)
            pred = torch.argmax(pred, dim=1)

            pred = pred.cpu().numpy().tolist()
            label = batch_label.cpu().numpy().tolist()

            all_pred.extend(pred)
            all_true.extend(label)

    accuracy = accuracy_score(all_true, all_pred)
    precision = precision_score(all_true, all_pred, average="micro")
    recall = recall_score(all_true, all_pred, average="micro")
    f1 = f1_score(all_true, all_pred, average="micro")

    logging.info(f"test dataset accuracy:{accuracy:.4f}\tprecision:{precision:.4f}\trecall:{recall:.4f}\tf1:{f1:.4f}")


if __name__ == "__main__":
    test_data()
