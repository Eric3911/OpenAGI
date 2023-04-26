ģ�Ͷ�δ���е��Σ�δ��ʹģ�͵�׼ȷ�ʴﵽ���
# ��Ŀ���ƣ�
ʹ�� Bert ģ�����Խ���ʵ��ʶ��

# ��Ŀ������
pytorch��python   
��ؿⰲװ
`pip install -r requirement.txt`

# ��ĿĿ¼��
```
bert ʵ��ʶ��
    |-- bert-base-chinese    bert ����Ԥѵ��ģ��     
    |-- data                 ���ݼ�               
    |-- model                �����ģ��               
    |-- config.py            �����ļ�                                 
    |-- main.py              ������                      
    |-- MyModel.py           ģ���ļ�                     
    |-- predict.py           Ԥ���ļ�                         
    |-- requirement.txt      ��Ҫ�İ�װ��
    |-- utils.py             ���ݴ����ļ�
```

# ��Ŀ���ݼ�
���ݼ��õ�������[��ACL 2018Chinese NER using Lattice LSTM��](https://github.com/jiesutd/LatticeLSTM)�д����˲ƾ��ռ��ļ������ݡ�

# ģ��ѵ��
`python main.py`

# ģ��Ԥ��
`python predict.py`

# Bert �ı����� �� Bert ʵ��ʶ�� ������
�ı����ಿ�ִ���
```
hidden_out = self.bert(input_ids, attention_mask=attention_mask,
                       output_hidden_states=False)  # �����Ƿ��������encoder��Ľ��
# shape (batch_size, hidden_size)  pooler_output -->  hidden_out[0]
pred = self.linear(hidden_out.pooler_output)
```
ʵ��ʶ�𲿷ִ���
```
bert_out = self.bert(batch_index)
bert_out0, bert_out1 = bert_out[0], bert_out[1]
pre = self.classifier(bert_out0)
```
���Կ������������Բ�����ݲ�ͬ��һ����`last_hidden_state(batch_size, sequence_length, hidden_size)`����һ����`pooler_output(batch_size, hidden_size)`��          
last_hidden_state��ģ�����һ�����������״̬���С�     
pooler_output����ͨ�����ڸ���Ԥѵ������Ĳ���н�һ����������еĵ�һ����ǣ������ǣ������һ������״̬��
���� BERT ϵ��ģ�ͣ������ͨ�����Բ�� tanh ���������󷵻ط����ǡ����Բ�Ȩ����Ԥѵ���ڼ����һ������Ԥ�⣨���ࣩĿ�����ѵ����         
�൱�� `last_hidden_state` �����ַ�����һ���ı�������`sequence_length`���֣�ÿ������`hidden_size`��һ���ֱ�ʾ��
`pooler_output` ����ƪ�¼��𣨷��ࣩ��һ���ı���`hidden_size`��ʾ
