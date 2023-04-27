# 项目名称：
使用 Word2Vec 来训练字&词向量并使用

# 项目环境：
python   
相关库安装
`pip install -r requirement.txt`

# 项目目录：
```
Word2Vec      
    |-- data                     数据集                              
    |-- main.py                  主函数
    |-- requiremeny.txt          相关库
    |-- word.model               字训练模型
    |-- word_data.vector         字向量
    |-- WordPartialWeight.pkl    单独保存的字的数据
    |-- words.model              词训练模型
    |-- words_data.vector        词向量
    |-- WordsPartialWeight.pkl   单独保存的词的数据
```

# 项目数据集
数据集使用THUCNews中的train.txt、test.txt、dev.txt 中所有的文本数据，一共有 20000 条

# 模型训练与查看
`python main.py`

# 博客地址
[CSDN](https://blog.csdn.net/qq_48764574/article/details/126350812)
