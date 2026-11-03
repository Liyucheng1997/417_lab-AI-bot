/* ================================================================
   avatar.js — 欢迎页的 3D 数字分身（大头娃娃 · 商务小孩）
   取自个人网站 liyucheng.me 的 assets/avatar3d-doll.js（模型 avatar-kid.glb、胸前 UNIMORE 校徽原样）。
   在这里多了：Bot 工作时环顾四周（setBusy）、面板收起或进入对话时暂停渲染（pause / resume）。
   ================================================================ */
import * as THREE from 'three';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
import { MeshoptDecoder } from 'three/addons/libs/meshopt_decoder.module.js';

export async function initAvatar3D(container, opts = {}){
  const modelUrl = opts.modelUrl || './avatar-doll.glb';

  const renderer = new THREE.WebGLRenderer({
    antialias: true, alpha: true,
    preserveDrawingBuffer: !!opts.preserveDrawingBuffer,
    powerPreference: 'low-power'
  });
  renderer.setClearColor(0x000000, 0);
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.domElement.style.cssText = 'position:absolute;inset:0;width:100%;height:100%;display:block';
  container.appendChild(renderer.domElement);

  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(28, 1, .05, 30);

  /* 打光偏亮偏软：贴图保持原样，白皙感靠灯光给（对齐 Tripo 预览的棚光观感） */
  scene.add(new THREE.HemisphereLight(0xffffff, 0xe4ddd4, 1.7));
  const key = new THREE.DirectionalLight(0xfff4ea, 1.15);
  key.position.set(1, 2.2, 2.5);
  scene.add(key);
  const fill = new THREE.DirectionalLight(0xeaf1ff, .55);
  fill.position.set(-1.5, .8, 1.2);
  scene.add(fill);

  /* ---------- 加载 GLB ---------- */
  const loader = new GLTFLoader();
  loader.setMeshoptDecoder(MeshoptDecoder);      /* GLB 用 meshopt 压缩（36 万面 → 2.9MB） */
  const gltf = await loader.loadAsync(modelUrl);
  const root = gltf.scene;
  scene.add(root);
  root.traverse(o => {
    if (o.isMesh || o.isSkinnedMesh){
      o.frustumCulled = false;
      /* 双面渲染：减面后个别微缝不至于漏出背景 */
      for (const m of (Array.isArray(o.material) ? o.material : [o.material])) m.side = THREE.DoubleSide;
    }
  });

  /* 骨骼与眼皮（名字来自绑骨脚本） */
  const bone = n => root.getObjectByName(n);

  /* 可选胸章：opts.badge = { url, size, offset:[x,y,z] } —— 别针风格圆徽，挂在胸骨上随呼吸起伏 */
  if (opts.badge && opts.badge.url){
    try {
      const tex = await new THREE.TextureLoader().loadAsync(opts.badge.url);
      tex.colorSpace = THREE.SRGBColorSpace;
      tex.anisotropy = 4;
      const R = opts.badge.size || .017;
      const pin = new THREE.Group();
      const back = new THREE.Mesh(
        new THREE.CylinderGeometry(R * 1.04, R * 1.04, R * .22, 32),
        new THREE.MeshStandardMaterial({ color: 0xd9dde5, roughness: .3, metalness: .85 })
      );
      back.rotation.x = Math.PI / 2;
      back.position.z = -R * .12;
      const face = new THREE.Mesh(
        new THREE.CircleGeometry(R, 32),
        new THREE.MeshStandardMaterial({ map: tex, roughness: .38, metalness: .05 })
      );
      face.position.z = .0006;
      pin.add(back, face);
      const chest = bone('Chest');
      chest.add(pin);
      root.updateMatrixWorld(true);
      const off = opts.badge.offset || [.055, .085, .108];
      const chestW = chest.getWorldPosition(new THREE.Vector3());
      const target = new THREE.Vector3(chestW.x + off[0], chestW.y + off[1], chestW.z + off[2]);
      pin.position.copy(chest.worldToLocal(target.clone()));
      pin.quaternion.copy(chest.getWorldQuaternion(new THREE.Quaternion()).invert());
      pin.rotateX(-.1);                            /* 贴合胸口微倾 */
    } catch (e){ console.warn('badge 加载失败', e); }
  }
  const B = {
    hips: bone('Hips'), spine: bone('Spine'), chest: bone('Chest'),
    neck: bone('Neck'), head: bone('Head'),
    lUp: bone('UpperArm_L'), rUp: bone('UpperArm_R'),
    lLow: bone('LowerArm_L'), rLow: bone('LowerArm_R'),
    rHand: bone('Hand_R'), rSh: bone('Shoulder_R')
  };
  const lids = [bone('Eyelid_L'), bone('Eyelid_R')].filter(Boolean);
  lids.forEach(l => l.scale.setScalar(.02));       /* 默认睁眼：眼皮缩到不可见 */

  /* 静止姿态四元数备份；世界系增量旋转（避免逐骨骼推局部轴向） */
  const rest = new Map();
  root.updateMatrixWorld(true);
  for (const k in B) if (B[k]) rest.set(B[k], B[k].quaternion.clone());
  const qTmp = new THREE.Quaternion(), qDelta = new THREE.Quaternion(), eTmp = new THREE.Euler();
  function setWorldDelta(node, rx, ry, rz){
    if (!node) return;
    node.parent.getWorldQuaternion(qTmp);          /* 自动向上刷新矩阵 */
    qDelta.setFromEuler(eTmp.set(rx, ry, rz, 'YXZ'));
    node.quaternion.copy(qTmp).invert().multiply(qDelta).multiply(qTmp).multiply(rest.get(node));
  }

  /* ---------- 取景：半身胸像（大头娃娃头占身高 1/3） ---------- */
  let headWorldY = .66;
  function resize(){
    const w = container.clientWidth || 1, h = container.clientHeight || 1;
    renderer.setPixelRatio(Math.min(devicePixelRatio || 1, 2));
    renderer.setSize(w, h, false);
    camera.aspect = w / h;
    root.updateMatrixWorld(true);
    headWorldY = B.head.getWorldPosition(new THREE.Vector3()).y;
    const top = headWorldY + .40, bottom = headWorldY - .30;   /* 发顶 ~ 皮带上方 */
    const fitH = top - bottom, cy = (top + bottom) / 2;
    const tan = Math.tan(THREE.MathUtils.degToRad(camera.fov / 2));
    const distV = fitH / (2 * tan);
    const distH = .55 / (2 * tan * camera.aspect);
    const dist = Math.max(distV, distH) * 1.06;
    camera.position.set(0, cy + .02, dist);
    camera.lookAt(0, cy, 0);
    camera.updateProjectionMatrix();
  }
  resize();

  /* ---------- 指针 ---------- */
  const pointer = { x: 0, y: 0, active: false };
  function onMove(e){
    const t = e.touches ? e.touches[0] : e;
    pointer.x = (t.clientX / innerWidth) * 2 - 1;
    pointer.y = (t.clientY / innerHeight) * 2 - 1;
    pointer.active = true;
  }
  addEventListener('mousemove', onMove, { passive: true });

  /* 挥手动作已按需求移除（腋下几何粘连，抬臂必破相）；保留空函数兼容外部调用 */
  function wave(){}

  let busy = false;
  function setBusy(b){ busy = !!b; }

  /* ---------- 帧循环 ---------- */
  const lerp = THREE.MathUtils.lerp;
  const clamp = THREE.MathUtils.clamp;
  let t = 0, blinkT = 0, nextBlink = 2 + Math.random() * 3;
  let curYaw = 0, curPitch = 0;
  let lastW = 0, lastH = 0;

  function frame(dt){
    dt = Math.min(dt, .05);
    t += dt;
    const cw = container.clientWidth, ch = container.clientHeight;
    if (cw !== lastW || ch !== lastH){ lastW = cw; lastH = ch; resize(); }

    /* --- 目光方向：跟鼠标 or 环顾 --- */
    let tx, ty;
    if (busy){ tx = Math.sin(t * 1.6) * .8; ty = -.25 + Math.sin(t * 2.3) * .12; }
    else if (pointer.active){ tx = pointer.x; ty = pointer.y; }
    else { tx = Math.sin(t * .3) * .5; ty = Math.sin(t * .21 + 1.7) * .25; }

    const wantYaw = clamp(tx * .5, -.5, .5);        /* 屏幕右 = 世界 +X → 绕 +Y 转负角看向它 */
    const wantPitch = clamp(ty * .28, -.25, .3);
    curYaw = lerp(curYaw, wantYaw, 1 - Math.exp(-dt * 5));
    curPitch = lerp(curPitch, wantPitch, 1 - Math.exp(-dt * 5));

    /* --- 待机：呼吸 + 轻微摇摆（世界系增量，从根到梢依次应用） --- */
    const breath = Math.sin(t * 1.7);
    setWorldDelta(B.hips, 0, 0, Math.sin(t * .55 + 1) * .012);
    setWorldDelta(B.spine, 0, Math.sin(t * .4) * .035, 0);
    setWorldDelta(B.chest, breath * .02, 0, 0);
    setWorldDelta(B.neck, curPitch * .35, curYaw * .35, 0);
    setWorldDelta(B.head, curPitch, curYaw, Math.sin(t * .45) * .02);

    /* --- 手臂：只保留呼吸微摆 --- */
    const sw = Math.sin(t * 1.7) * .025;
    setWorldDelta(B.lUp, 0, 0, sw);
    setWorldDelta(B.lLow, 0, 0, 0);
    setWorldDelta(B.rUp, 0, 0, -sw);
    setWorldDelta(B.rLow, 0, 0, 0);
    setWorldDelta(B.rHand, 0, 0, 0);

    /* --- 眨眼：眼皮盖片从眼心放大盖住眼睛 --- */
    blinkT += dt;
    let blink = 0;
    if (blinkT > nextBlink){
      const p = (blinkT - nextBlink) / .16;
      if (p >= 1){ blinkT = 0; nextBlink = 1.8 + Math.random() * 3.5; }
      else blink = Math.sin(p * Math.PI);
    }
    const s = Math.max(blink, .02);
    lids.forEach(l => l.scale.setScalar(s));

    renderer.render(scene, camera);
  }

  function dispose(){
    removeEventListener('mousemove', onMove);
    renderer.dispose();
    if (renderer.domElement.parentNode === container) container.removeChild(renderer.domElement);
  }

  /* 自带渲染循环，可暂停（面板收起 / 进入对话时不再占 GPU） */
  let running = true, last = performance.now();
  function loop(now){
    if (!running) return;
    frame((now - last) / 1000);
    last = now;
    requestAnimationFrame(loop);
  }
  requestAnimationFrame(loop);
  function pause(){ running = false; }
  function resume(){ if (!running){ running = true; last = performance.now(); requestAnimationFrame(loop); } }

  return { frame, resize, dispose, wave, setBusy, pause, resume, renderer, scene, camera };
}
