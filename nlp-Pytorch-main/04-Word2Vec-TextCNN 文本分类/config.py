# -*- coding:utf-8 -*-
# @author: 木子川
# @Data:   2022/8/1
# @Email:  m21z50c71@163.com

import argparse
import os.path


def parsers():
    parser = argparse.ArgumentParser(description="TextCNN model of argparse")
    parser.add_argument("--train_file", type=str, default=os.path.join("data", "train.txt"))
    parser.add_argument("--dev_file", type=str, default=os.path.join("data", "dev.txt"))
    parser.add_argument("--test_file", type=str, default=os.path.join("data", "test.txt"))
    parser.add_argument("--classification", type=str, default=os.path.join("data", "class.txt"))
    parser.add_argument("--data_word_model", type=str, default=os.path.join("data", "word.bin"))
    parser.add_argument("--data_words_model", type=str, default=os.path.join("data", "words.bin"))
    parser.add_argument("--word", type=bool, default=False)
    parser.add_argument("--class_num", type=int, default=10)
    parser.add_argument("--max_len", type=int, default=38)
    parser.add_argument("--embedding_num", type=int, default=50)
    parser.add_argument("--batch_size", type=int, default=32)
    parser.add_argument("--epochs", type=int, default=10)
    parser.add_argument("--learn_rate", type=float, default=1e-3)
    parser.add_argument("--num_filters", type=int, default=2, help="卷积产生的通道数")
    parser.add_argument("--save_word_model_best", type=str, default=os.path.join("model", "word_best_model.pth"))
    parser.add_argument("--save_word_model_last", type=str, default=os.path.join("model", "word_last_model.pth"))
    parser.add_argument("--save_words_model_best", type=str, default=os.path.join("model", "words_best_model.pth"))
    parser.add_argument("--save_words_model_last", type=str, default=os.path.join("model", "words_last_model.pth"))
    args = parser.parse_args()
    return args
