"""Creative model precision profiles and their weight sources."""
MEDIA = {
    'video': {'model': 'minimax-h3', 'bundle': 'h3-video-g4-v1', 'template': 'h3-int8.json',
              'weight_source': 'hf', 'manifest': 'h3-video-weights.json', 'name': 'H3 视频'},
    'image': {'model': 'minimax-image-bf16', 'bundle': None, 'template': 'h3-image-bf16.json',
              'weight_source': 'hf', 'manifest': 'h3-image-bf16-weights.json', 'name': 'H3 图片'},
}


def media_preset(model):
    for item in MEDIA.values():
        if item['model'] == model:
            return item
    raise ValueError('未知创作模型')


def graph_model(graph):
    kinds = {node['class_type'] for node in graph.values()}
    if 'MiniMaxH3ImageConditioning' in kinds:
        if 'MiniMaxH3ImageToVideo' in kinds:
            raise ValueError('图片与视频请分别提交工作流')
        if not any(node['class_type'] == 'UNETLoader' and node['inputs'].get('unet_name') == 'minimax_h3_image_pruned_bf16.safetensors' for node in graph.values()):
            raise ValueError('H3 图片仅支持 BF16 主模型，请重新载入图片配置')
        if not any(node['class_type'] == 'CLIPLoader' and node['inputs'].get('clip_name') == 'qwen3vl_32b_minimax_h3_bf16.safetensors' for node in graph.values()):
            raise ValueError('BF16 图片配置需要匹配 BF16 文本编码器')
        return 'minimax-image-bf16'
    return 'minimax-h3'
