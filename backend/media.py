"""The two supported creative presets and their required cloud bundles."""
MEDIA = {
    'video': {'model': 'minimax-h3', 'bundle': 'h3-video-g4-v1', 'template': 'h3-int8.json', 'name': 'H3 视频'},
    'image': {'model': 'minimax-image', 'bundle': 'h3-image-g4-v1', 'template': 'h3-image.json', 'name': 'H3 图片'},
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
        return 'minimax-image'
    return 'minimax-h3'
