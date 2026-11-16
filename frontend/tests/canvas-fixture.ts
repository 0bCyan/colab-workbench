import { initialDemo, makeNode } from '../src/canvas/model'

export function exampleCanvas() {
  const demo = initialDemo(['colab1', 'colab2', 'colab3'])
  return { ...demo, activeId: 'demo-images', boards: [
    { id: 'demo-images', name: '图片探索', instanceId: 'demo-g4-1', edges: [], nodes: [
      makeNode('image', 0, '晨雾中的山野，柔和的日光，安静的绿色。', '晨雾'),
      makeNode('image', 1, '湖畔远山，水面倒影，淡雅的自然色彩。', '远山'),
      makeNode('image', 2, '树影与微风，午后光线穿过层叠的叶片。', '树影'),
    ] },
    { id: 'demo-video', name: '视频分镜', instanceId: 'demo-g4-1', edges: [], nodes: [
      makeNode('video', 0, '镜头缓缓掠过森林，薄雾在晨光中流动。', '森林漫游'),
      makeNode('video', 1, '固定镜头，湖面涟漪轻轻扩散，远处云层移动。', '湖面涟漪'),
    ] },
  ] }
}
