模型都未进行调参，未能使模型的准确率达到最高
# 项目名称：
使用 TextCNN 模型来对中文进行分类，即文本分类

# 项目环境：
pytorch、python   
相关库安装
`pip install -r requirement.txt`

# 项目目录：
```
TextCNN         
    |-- data                 数据集   
    |-- model                保存的模型               
    |-- config.py            配置文件                    
    |-- main.py              主函数                      
    |-- model.py             模型文件                     
    |-- predict.py           预测文件                         
    |-- requirement.txt      需要的安装包   
    |-- TextCNN.pdf          TextCNN 的论文
    |-- utils.py             数据处理文件
   ```

# 模型介绍
详细内容请看[TextCNN 文本分类介绍](../01-TextCNN%20文本分类/README.md)

# 修改部分
相对于原始 TextCNN 模型的 Emdedding 层，此项目用了 Word2Vec 来代替。
关于 Word2Vec 训练得到词向量，可以看[Word2Vec 字&词向量](../00-Word2Vec%20字&词向量)

```
# 添加 "<pad>" 和 "<UNK>"
# {"<PAD>": np.zeros(self.embedding), "<UNK>": np.random.randn(self.embedding)}
self.Embedding = self.model.vectors
self.Embedding = np.insert(self.Embedding, self.num_word, [np.zeros(self.embedding), np.random.randn(self.embedding)], axis=0)

self.word_2_index = self.model.key_to_index
self.word_2_index.update({"<PAD>": self.num_word, "<UNK>": self.num_word + 1})
```

```
text_id = [self.word_2_index.get(i, self.word_2_index["<UNK>"]) for i in text]
        text_id = text_id + [self.word_2_index["<PAD>"]] * (self.max_len - len(text_id))

wordEmbedding = np.array([self.Embedding[i] for i in text_id])

text_id = torch.tensor(wordEmbedding).unsqueeze(dim=0)
```

# 模型训练
`python main.py`

# 模型预测
`python predict.py`

