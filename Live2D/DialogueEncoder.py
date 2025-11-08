import torch
import torch.nn as nn
from transformers import BertModel, BertTokenizer
from typing import List, Tuple

class DialogueEncoder(nn.Module):
    """
    升级版对话编码器 (结构化上下文 -> 向量)。
    
    职责: 将一个包含多轮对话元组的列表转换为单一的语义向量。
    """
    def __init__(self, model_name: str = 'bert-base-chinese'):
        super().__init__()
        self.tokenizer = BertTokenizer.from_pretrained(model_name)
        self.bert_model = BertModel.from_pretrained(model_name)
        self.output_dim = self.bert_model.config.hidden_size

    @staticmethod
    def _format_dialogue(history: List[Tuple[str, str]]) -> str:
        """
        一个静态辅助方法，用于将结构化的对话历史格式化为单个字符串。
        """
        # 使用换行符和说话人标识来构建上下文
        formatted_turns = [f"User: {user_text}\nAI: {ai_text}" for user_text, ai_text in history]
        # 使用两个换行符来清晰地分隔不同的轮次
        return "\n\n".join(formatted_turns)

    def forward(self, conversation_histories: List[List[Tuple[str, str]]]) -> torch.Tensor:
        """
        前向传播函数。
        
        参数:
        - conversation_histories: 一个批次的对话历史。
          每个对话历史是一个列表，包含一个或多个 (用户话语, AI话语) 的元组。
          例如: [
              [("你好", "你好呀")],  # 第一个样本，1轮对话
              [("你多大了？", "这是个秘密。"), ("好吧", "嘻嘻")]  # 第二个样本，2轮对话
          ]
        
        返回:
        - torch.Tensor: 一个形状为 [len(conversation_histories), 768] 的张量。
        """
        # 1. 将结构化输入格式化为字符串列表
        formatted_texts = [self._format_dialogue(history) for history in conversation_histories]
        
        # 2. 文本预处理：使用分词器
        inputs = self.tokenizer(
            formatted_texts,
            padding=True,
            truncation=True,
            max_length=512,
            return_tensors='pt'
        )
        
        # 3. 将数据移动到模型所在的设备
        inputs = {key: val.to(self.bert_model.device) for key, val in inputs.items()}
        
        # 4. 模型推理
        with torch.no_grad():
            outputs = self.bert_model(**inputs)
        
        # 5. 提取[CLS] token对应的向量作为整个上下文的语义表示
        dialogue_vector = outputs.last_hidden_state[:, 0, :]
        
        return dialogue_vector, formatted_texts

# --- 独立运行和输出示例 ---
if __name__ == '__main__':
    # 1. 实例化模块
    print("正在加载 DialogueEncoder 模型...")
    encoder = DialogueEncoder()
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    encoder.to(device)
    encoder.eval()
    print(f"模型已加载到 {device}。")

    # 2. 准备符合新格式的输入数据（一个批次包含3个对话样本）
    sample_batch = [
        # 样本1: 1轮对话，表示高兴和欢迎
        [
            ("今天天气真好！", "是呀，心情都变好了呢！")
        ],
        
        # 样本2: 2轮对话，表示关心和安慰
        [
            ("我今天遇到了一些不开心的事情。", "别难过，和我说说吧，也许会好受一些。"),
            ("谢谢你。", "没关系，我一直都在。")
        ],

        # 样本3: 1轮对话，中性/陈述事实
        [
            ("介绍一下你自己。", "我是一个大型语言模型。")
        ]
    ]

    print("\n--- 输入数据示例 ---")
    for i, history in enumerate(sample_batch):
        print(f"样本 {i+1} (包含 {len(history)} 轮对话): {history}")

    # 3. 使用模块进行转换
    semantic_vectors, formatted_inputs = encoder(sample_batch)
    
    print("\n--- 模型内部处理的格式化文本 ---")
    for i, text in enumerate(formatted_inputs):
        print(f"样本 {i+1} 格式化后:\n---\n{text}\n---")

    # 4. 检查输出
    print("\n--- 输出结果 ---")
    print(f"输出向量的形状: {semantic_vectors.shape}")
    print(f"解释: {semantic_vectors.shape[0]} 个对话样本, 每个样本被转换成了一个 {semantic_vectors.shape[1]} 维的语义向量。")
    
    print("\n--- 单个输出向量示例 ---")
    print(f"样本1 ('高兴') 对应的向量 (前10个值):")
    print(semantic_vectors[0, :10].tolist())

    print(f"\n样本2 ('安慰') 对应的向量 (前10个值):")
    print(semantic_vectors[1, :10].tolist())
    
    print(f"\n样本3 ('中性') 对应的向量 (前10个值):")
    print(semantic_vectors[2, :10].tolist())