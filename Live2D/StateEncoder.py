import torch
import torch.nn as nn

class StateEncoder(nn.Module):
    """
    状态编码器模块 (Live2D当前位置 -> 状态向量)。
    
    职责: 将输入的Live2D原始参数向量转换为一个浓缩的状态特征向量。
    """
    def __init__(self, input_dim: int, output_dim: int = 64):
        """
        初始化函数。
        
        参数:
        - input_dim: 输入向量的维度，即Live2D参数的数量。
        - output_dim: 输出的特征向量维度。
        """
        super().__init__()
        
        self.input_dim = input_dim
        self.output_dim = output_dim
        
        # 使用 nn.Sequential 可以方便地将多个层组合成一个模块
        self.encoder = nn.Sequential(
            nn.Linear(input_dim, 128),  # 第一个线性层，放大特征
            nn.ReLU(),                 # ReLU激活函数，引入非线性
            nn.Linear(128, output_dim) # 第二个线性层，压缩到目标维度
        )

    def forward(self, current_states: torch.Tensor) -> torch.Tensor:
        """
        前向传播函数。
        
        参数:
        - current_states: 一个形状为 [batch_size, input_dim] 的张量。
        
        返回:
        - torch.Tensor: 一个形状为 [batch_size, output_dim] 的状态特征向量。
        """
        
        # 验证输入形状是否正确 (这是一个很好的防御性编程习惯)
        if current_states.shape[-1] != self.input_dim:
            raise ValueError(
                f"输入张量的最后一个维度应为 {self.input_dim}, "
                f"但收到了 {current_states.shape[-1]}"
            )
            
        state_vector = self.encoder(current_states)
        return state_vector

# --- 独立运行和输出示例 ---
if __name__ == '__main__':
    # 假设我们要控制50个Live2D参数
    NUM_LIVE2D_PARAMS = 50
    # 我们希望输出一个64维的特征向量
    STATE_FEATURE_DIM = 64
    
    # 1. 实例化我们的模块
    print(f"正在创建 StateEncoder...")
    state_encoder = StateEncoder(input_dim=NUM_LIVE2D_PARAMS, output_dim=STATE_FEATURE_DIM)
    print("创建完成！")
    
    # 2. 准备一个批次的伪数据
    # 假设批次大小为4，即同时处理4个样本
    BATCH_SIZE = 4
    # 随机生成一些Live2D参数值，通常这些值在使用前会被归一化到-1到1或0到1之间
    # 这里我们用 torch.rand 创建0到1之间的随机数
    fake_live2d_states = torch.rand(BATCH_SIZE, NUM_LIVE2D_PARAMS)
    
    print(f"\n创建了一个批次的伪输入数据，形状为: {fake_live2d_states.shape}")
    print(f"解释: {BATCH_SIZE} 个样本, 每个样本有 {NUM_LIVE2D_PARAMS} 个Live2D参数。")
    
    # 3. 使用模块进行转换
    state_feature_vectors = state_encoder(fake_live2d_states)
    
    # 4. 检查输出
    print(f"\n经过 StateEncoder 处理后，输出的特征向量形状为: {state_feature_vectors.shape}")
    print(f"解释: {state_feature_vectors.shape[0]} 个样本, 每个样本被转换成了一个 {state_feature_vectors.shape[1]} 维的状态特征向量。")

    print("\n模块工作正常！")