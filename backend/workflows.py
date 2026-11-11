import json
import math


def validate_graph(graph):
    if not isinstance(graph, dict) or not graph or len(graph) > 500:
        raise ValueError("工作流必须是 1–500 个节点的 ComfyUI API JSON。请在 ComfyUI 中导出 API 格式。")
    if len(json.dumps(graph)) > 2_000_000:
        raise ValueError("工作流过大")
    edges = {}
    for key, node in graph.items():
        if not isinstance(key, str) or not isinstance(node, dict):
            raise ValueError("节点 ID 和节点格式无效")
        if not isinstance(node.get("class_type"), str) or not isinstance(node.get("inputs"), dict):
            raise ValueError(f"节点 {key} 缺少 class_type 或 inputs；这里需要 API 格式")
        edges[key] = []
        for field, value in node["inputs"].items():
            if isinstance(value, list) and len(value) == 2 and isinstance(value[0], str):
                if value[0] not in graph or type(value[1]) is not int or value[1] < 0:
                    raise ValueError(f"节点 {key}.{field} 引用了不存在的节点或输出")
                edges[key].append(value[0])
            elif isinstance(value, float) and not math.isfinite(value):
                raise ValueError(f"节点 {key}.{field} 必须是有限数值")
        inputs = node["inputs"]
        if node["class_type"] == "MiniMaxH3ImageToVideo":
            for field in ("width", "height"):
                value = inputs.get(field)
                if type(value) is not int or value < 256 or value > 2048 or value % 64:
                    raise ValueError(f"{field} 必须是 256–2048 内的 64 的倍数")
            value = inputs.get("length")
            if type(value) is not int or not 4 <= value <= 512 or value % 4:
                raise ValueError("帧数必须是 4–512 内的 4 的倍数")
        if node["class_type"] == "BasicScheduler":
            steps = inputs.get("steps")
            if type(steps) is not int or not 1 <= steps <= 100:
                raise ValueError("采样步数必须在 1–100 之间")
        if node["class_type"] == "RandomNoise":
            seed = inputs.get("noise_seed")
            if type(seed) is not int or not 0 <= seed <= 2**53 - 1:
                raise ValueError("随机种子必须是非负安全整数")
        if node["class_type"] == "CreateVideo":
            fps = inputs.get("fps")
            if type(fps) not in (int, float) or not 1 <= fps <= 120:
                raise ValueError("帧率必须在 1–120 之间")
    visited, active = set(), set()

    def visit(key):
        if key in active:
            raise ValueError("工作流存在循环连接")
        if key in visited:
            return
        active.add(key)
        for dependency in edges[key]:
            visit(dependency)
        active.remove(key)
        visited.add(key)

    for key in graph:
        visit(key)
    return graph
