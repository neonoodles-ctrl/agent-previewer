// Previewer's 3D viewer: shaded, textured models on three.js with a file check,
// a print check, animation playback and a frozen view for notes. app.js imports it when a model opens.
import * as THREE from "./vendor/three/three.module.js";
import { OrbitControls } from "./vendor/three/addons/controls/OrbitControls.js";
import { GLTFLoader } from "./vendor/three/addons/loaders/GLTFLoader.js";
import { OBJLoader } from "./vendor/three/addons/loaders/OBJLoader.js";
import { STLLoader } from "./vendor/three/addons/loaders/STLLoader.js";
import { ThreeMFLoader } from "./vendor/three/addons/loaders/3MFLoader.js";
import { unzipSync } from "./vendor/three/addons/libs/fflate.module.js";
import { RoomEnvironment } from "./vendor/three/addons/environments/RoomEnvironment.js";
import { STLExporter } from "./vendor/three/addons/exporters/STLExporter.js";

export const MODES = ["textured", "clay", "wireframe"];
// glTF counts in metres and 3MF names its unit. STL and OBJ name none; slicers read them as millimetres.
const THREE_MF_MM = { micron: 0.001, millimeter: 1, centimeter: 10, inch: 25.4, foot: 304.8, meter: 1000 };
const MAP_SLOTS = [["map", "Colour"], ["metalnessMap", "Metal"], ["roughnessMap", "Roughness"], ["normalMap", "Surface detail"], ["aoMap", "Occlusion"], ["emissiveMap", "Glow"]];

export async function mount(canvas, { url, format }) {
  const response = await fetch(url);
  if (!response.ok) throw new Error(`The model did not load (${response.status}).`);
  const buffer = await response.arrayBuffer();
  const kind = sniff(buffer, format);
  const { root, animations, mmPerUnit, zUp, formatLabel, startMode = "textured" } = await parse(buffer, kind, url);

  const scene = new THREE.Scene();
  scene.background = new THREE.Color(0x15191e);
  const pivot = new THREE.Group();
  if (zUp) pivot.rotation.x = -Math.PI / 2;
  pivot.add(root);
  scene.add(pivot);
  scene.updateMatrixWorld(true);
  const meshes = [];
  root.traverse((node) => { if (node.isMesh) meshes.push(node); });
  // STL facet normals are often zero and OBJ may have none; clay lights from the geometry's normals.
  meshes.forEach((mesh) => { if (kind === "stl" || !mesh.geometry.hasAttribute("normal")) mesh.geometry.computeVertexNormals(); });
  const box = new THREE.Box3().setFromObject(pivot, true);
  // Checked before the renderer exists, so a file with nothing to draw holds no WebGL context.
  if (!meshes.length || box.isEmpty()) throw new Error("The file has no visible mesh.");
  const sphere = box.getBoundingSphere(new THREE.Sphere());
  const radius = sphere.radius || 1;

  const renderer = new THREE.WebGLRenderer({ canvas, antialias: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  renderer.toneMapping = THREE.NeutralToneMapping;
  // Below 1 so white and pale models keep their shading instead of clipping to flat white.
  renderer.toneMappingExposure = 0.8;
  const pmrem = new THREE.PMREMGenerator(renderer);
  const environment = pmrem.fromScene(new RoomEnvironment(), 0.04).texture;
  scene.environment = environment;
  // The room light is even all round; one key light gives clay and wireframe some shape.
  const key = new THREE.DirectionalLight(0xffffff, 0.7);

  const camera = new THREE.PerspectiveCamera(40, 1, radius / 100, radius * 100);
  const controls = new OrbitControls(camera, canvas);
  controls.enableDamping = true;
  controls.minDistance = radius * 0.05;
  controls.maxDistance = radius * 20;
  controls.autoRotateSpeed = 2;
  controls.addEventListener("start", () => { touched = true; });
  // The key light rides on the camera, above and to the right, aimed at the model's centre.
  const keyTarget = new THREE.Object3D();
  keyTarget.position.copy(sphere.center);
  scene.add(keyTarget);
  key.target = keyTarget;
  camera.add(key);
  key.position.set(1, 2, 1.5);
  scene.add(camera);

  const size = box.getSize(new THREE.Vector3());
  const sizeMm = { width: size.x * mmPerUnit, depth: size.z * mmPerUnit, height: size.y * mmPerUnit };
  const gridMm = niceStep(Math.max(sizeMm.width, sizeMm.depth, sizeMm.height) / 5);
  const gridCells = Math.max(4, Math.ceil(Math.max(sizeMm.width, sizeMm.depth) * 1.6 / gridMm / 2) * 2);
  const grid = new THREE.GridHelper(gridCells * gridMm / mmPerUnit, gridCells, 0x6b7785, 0x323a44);
  grid.position.set(sphere.center.x, box.min.y, sphere.center.z);
  scene.add(grid);

  const originals = new Map(meshes.map((mesh) => [mesh, mesh.material]));
  const clay = new THREE.MeshStandardMaterial({ color: 0xbfc4ca, roughness: 0.72, metalness: 0 });
  const wire = new THREE.MeshBasicMaterial({ color: 0x5eead4, wireframe: true });
  let mode = "textured";

  const mixer = animations.length ? new THREE.AnimationMixer(root) : null;
  let action = null;
  let playing = false;
  let frozen = false;
  let dirty = true;
  let disposed = false;
  let homed = false;
  // Until the reviewer moves the camera, the model is framed again whenever the viewer changes shape (Expand).
  let touched = false;
  let lastWidth = 0;
  let lastHeight = 0;
  let lastTime = performance.now();
  let worker = null;
  let stopCheck = null;
  let tick = null;

  const viewer = {
    kind,
    animations: animations.map((clip) => ({ name: clip.name || "Animation", duration: clip.duration })),
    report: fileReport(),
    sizeMm,
    gridMm,
    mmPerUnit,
    unitNote: unitNote(kind, mmPerUnit),
    get mode() { return mode; },
    get turntable() { return controls.autoRotate; },
    get grid() { return grid.visible; },
    get playing() { return playing; },
    get time() { return action ? action.time : 0; },
    get clipIndex() { return action ? animations.indexOf(action.getClip()) : -1; },
    onTick(callback) { tick = callback; },
    setMode(value) {
      if (!MODES.includes(value)) return;
      mode = value;
      meshes.forEach((mesh) => { mesh.material = value === "textured" ? originals.get(mesh) : value === "clay" ? clay : wire; });
      dirty = true;
    },
    setTurntable(on) { controls.autoRotate = Boolean(on); dirty = true; },
    setGrid(on) { grid.visible = Boolean(on); dirty = true; },
    resetView() { touched = false; home(); },
    // factor below 1 moves the camera in, above 1 out, within the same limits as the wheel.
    zoom(factor) {
      const offset = camera.position.clone().sub(controls.target);
      offset.setLength(Math.min(Math.max(offset.length() * factor, controls.minDistance), controls.maxDistance));
      camera.position.copy(controls.target).add(offset);
      controls.update();
      touched = true;
      dirty = true;
    },
    useClip(index) {
      if (!mixer || !animations[index]) return;
      if (action) action.stop();
      action = mixer.clipAction(animations[index]);
      action.play();
      action.paused = !playing;
      mixer.update(0);
      dirty = true;
    },
    setPlaying(on) { playing = Boolean(on) && Boolean(action); if (action) action.paused = !playing; },
    seek(seconds) {
      if (!action) return;
      action.time = Math.min(Math.max(Number(seconds) || 0, 0), action.getClip().duration);
      mixer.update(0);
      dirty = true;
    },
    // Notes keep the camera, the view mode and, for animated models, the clip and its time.
    view() {
      const view = { position: camera.position.toArray().map(round), target: controls.target.toArray().map(round), mode };
      if (action) view.clip = action.getClip().name || "Animation";
      return view;
    },
    setView(view, seconds = 0) {
      touched = true;
      controls.autoRotate = false;
      camera.position.fromArray(view.position);
      controls.target.fromArray(view.target);
      controls.update();
      if (view.mode) viewer.setMode(view.mode);
      if (view.clip !== undefined && mixer) {
        const index = animations.findIndex((clip) => (clip.name || "Animation") === view.clip);
        if (index >= 0) { viewer.setPlaying(false); viewer.useClip(index); viewer.seek(seconds); }
      }
      dirty = true;
    },
    freeze(on) {
      frozen = Boolean(on);
      controls.enabled = !frozen;
      if (frozen) controls.autoRotate = false;
      dirty = true;
    },
    // The frame as shown, for a note. Drawn straight after rendering, while the WebGL buffer still holds it.
    snapshot(maxWidth = 1920) {
      renderer.render(scene, camera);
      const scale = Math.min(1, maxWidth / canvas.width);
      const still = document.createElement("canvas");
      still.width = Math.round(canvas.width * scale);
      still.height = Math.round(canvas.height * scale);
      still.getContext("2d").drawImage(canvas, 0, 0, still.width, still.height);
      return still;
    },
    printCheck,
    // A binary STL in millimetres, Z up, for slicers that cannot read glTF.
    exportStl() {
      const holder = new THREE.Group();
      holder.rotation.x = Math.PI / 2;
      holder.scale.setScalar(mmPerUnit);
      pivot.remove(root);
      holder.add(root);
      holder.updateMatrixWorld(true);
      try { return new STLExporter().parse(holder, { binary: true }); }
      finally { holder.remove(root); pivot.add(root); pivot.updateMatrixWorld(true); }
    },
    dispose,
  };
  if (startMode !== "textured") viewer.setMode(startMode);
  if (mixer) { playing = true; viewer.useClip(0); }
  requestAnimationFrame(frame);
  return viewer;

  function frame(now) {
    if (disposed) return;
    if (!canvas.isConnected) { dispose(); return; }
    requestAnimationFrame(frame);
    const delta = Math.min((now - lastTime) / 1000, 0.1);
    lastTime = now;
    if (!canvas.clientWidth || !canvas.clientHeight) return;
    resize();
    if (!homed) { home(); homed = true; }
    let changed = controls.update(delta);
    if (mixer && action && playing && !frozen) {
      mixer.update(delta);
      changed = true;
      if (tick) tick(action.time);
    }
    if (changed || dirty) { renderer.render(scene, camera); dirty = false; }
  }

  function resize() {
    const width = canvas.clientWidth;
    const height = canvas.clientHeight;
    if (width === lastWidth && height === lastHeight) return;
    lastWidth = width;
    lastHeight = height;
    renderer.setSize(width, height, false);
    camera.aspect = width / height;
    camera.updateProjectionMatrix();
    if (homed && !touched && !controls.autoRotate) home();
    dirty = true;
  }

  function home() {
    const vertical = THREE.MathUtils.degToRad(camera.fov) / 2;
    const horizontal = Math.atan(Math.tan(vertical) * camera.aspect);
    const distance = radius / Math.sin(Math.min(vertical, horizontal)) * 1.1;
    camera.position.copy(sphere.center).addScaledVector(new THREE.Vector3(1, 0.65, 1.5).normalize(), distance);
    controls.target.copy(sphere.center);
    controls.update();
    dirty = true;
  }

  function fileReport() {
    let triangles = 0;
    let vertices = 0;
    const materials = new Set();
    meshes.forEach((mesh) => {
      const geometry = mesh.geometry;
      const count = geometry.index ? geometry.index.count : geometry.attributes.position.count;
      triangles += Math.floor(count / 3);
      vertices += geometry.attributes.position.count;
      (Array.isArray(mesh.material) ? mesh.material : [mesh.material]).forEach((material) => materials.add(material));
    });
    // glTF packs metal and roughness into one image, so maps that share an image are listed together.
    const textures = new Map();
    materials.forEach((material) => MAP_SLOTS.forEach(([slot, label]) => {
      const image = material[slot]?.image;
      if (!image) return;
      const entry = textures.get(image) || { labels: [], image, width: image.width, height: image.height };
      if (!entry.labels.includes(label)) entry.labels.push(label);
      textures.set(image, entry);
    }));
    return {
      format: formatLabel,
      bytes: buffer.byteLength,
      triangles,
      vertices,
      meshes: meshes.length,
      materials: materials.size,
      textures: [...textures.values()],
      animations: animations.map((clip) => ({ name: clip.name || "Animation", duration: clip.duration })),
    };
  }

  // World positions in millimetres go to a worker, so a million-triangle mesh does not stall the page.
  function printCheck() {
    let vertexTotal = 0;
    let indexTotal = 0;
    meshes.forEach((mesh) => {
      const geometry = mesh.geometry;
      vertexTotal += geometry.attributes.position.count;
      indexTotal += geometry.index ? geometry.index.count : geometry.attributes.position.count;
    });
    const positions = new Float32Array(vertexTotal * 3);
    const indices = new Uint32Array(indexTotal);
    const vector = new THREE.Vector3();
    let vertexOffset = 0;
    let indexOffset = 0;
    meshes.forEach((mesh) => {
      const geometry = mesh.geometry;
      const position = geometry.attributes.position;
      for (let index = 0; index < position.count; index += 1) {
        vector.fromBufferAttribute(position, index).applyMatrix4(mesh.matrixWorld).multiplyScalar(mmPerUnit);
        const at = (vertexOffset + index) * 3;
        positions[at] = vector.x;
        positions[at + 1] = vector.y;
        positions[at + 2] = vector.z;
      }
      const count = geometry.index ? geometry.index.count : position.count;
      for (let index = 0; index < count; index += 1) indices[indexOffset + index] = vertexOffset + (geometry.index ? geometry.index.getX(index) : index);
      vertexOffset += position.count;
      indexOffset += count;
    });
    return new Promise((resolve, reject) => {
      worker = new Worker(new URL("./print-check.js", import.meta.url));
      stopCheck = () => reject(new Error("The viewer was closed."));
      worker.onmessage = ({ data }) => { worker.terminate(); worker = null; resolve(data); };
      worker.onerror = (event) => { worker.terminate(); worker = null; reject(new Error(event.message || "The print check failed.")); };
      worker.postMessage({ positions, indices }, [positions.buffer, indices.buffer]);
    });
  }

  function dispose() {
    if (disposed) return;
    disposed = true;
    if (worker) { worker.terminate(); stopCheck(); }
    controls.dispose();
    const textures = new Set();
    root.traverse((node) => {
      if (!node.isMesh) return;
      node.geometry.dispose();
      (Array.isArray(originals.get(node)) ? originals.get(node) : [originals.get(node)]).forEach((material) => {
        MAP_SLOTS.forEach(([slot]) => { if (material[slot]) textures.add(material[slot]); });
        material.dispose();
      });
    });
    textures.forEach((texture) => texture.dispose());
    [clay, wire, environment, pmrem, grid.geometry, grid.material].forEach((resource) => resource.dispose());
    renderer.dispose();
    // Free the WebGL context now: browsers cap live contexts, and reviewers open model after model.
    renderer.forceContextLoss();
  }
}

function sniff(buffer, format) {
  const head = new Uint8Array(buffer, 0, Math.min(4, buffer.byteLength));
  if (String.fromCharCode(...head) === "glTF") return "glb";
  if (head[0] === 0x50 && head[1] === 0x4b) return "3mf";
  return format;
}

async function parse(buffer, kind, url) {
  if (kind === "glb" || kind === "gltf") {
    const loader = new GLTFLoader();
    // Firefox would fetch embedded textures from blob: URLs, which Previewer's connect-src forbids.
    // Loading them as <img> is allowed (img-src blob:), so give the parser an image loader.
    loader.register((parser) => { parser.textureLoader = new THREE.TextureLoader(parser.options.manager); return { name: "previewer_image_textures" }; });
    const data = kind === "glb" ? buffer : new TextDecoder().decode(buffer);
    let gltf;
    try { gltf = await new Promise((resolve, reject) => loader.parse(data, `${url}/`, resolve, reject)); }
    catch (error) { throw unsupported(error); }
    // A file with no materials gets glTF's default, plain white metal, which shows no shape; start in clay.
    const startMode = gltf.parser.json.materials?.length ? "textured" : "clay";
    return { root: gltf.scene, animations: gltf.animations, mmPerUnit: 1000, zUp: false, formatLabel: kind === "glb" ? "GLB (glTF 2.0)" : "glTF 2.0", startMode };
  }
  if (kind === "stl") {
    const geometry = new STLLoader().parse(buffer);
    const material = new THREE.MeshStandardMaterial({ color: 0xc8ccd2, roughness: 0.6, metalness: 0, vertexColors: geometry.hasAttribute("color") });
    const root = new THREE.Group();
    root.add(new THREE.Mesh(geometry, material));
    const binary = buffer.byteLength >= 84 && 84 + 50 * new DataView(buffer).getUint32(80, true) === buffer.byteLength;
    return { root, animations: [], mmPerUnit: 1, zUp: true, formatLabel: binary ? "STL (binary)" : "STL (text)" };
  }
  if (kind === "3mf") {
    const root = new ThreeMFLoader().parse(buffer);
    return { root, animations: [], mmPerUnit: threeMfUnit(buffer), zUp: true, formatLabel: "3MF" };
  }
  if (kind === "obj") {
    return { root: new OBJLoader().parse(new TextDecoder().decode(buffer)), animations: [], mmPerUnit: 1, zUp: false, formatLabel: "OBJ" };
  }
  throw new Error(`Previewer's viewer cannot open ${String(kind).toUpperCase()} files.`);
}

function unsupported(error) {
  const message = String(error?.message || error);
  if (message.includes("DRACOLoader")) return new Error("This GLB is Draco-compressed. Previewer's viewer does not open Draco files.");
  if (message.includes("KTX2Loader")) return new Error("This model uses KTX2 textures. Previewer's viewer does not open them.");
  if (message.includes("MeshoptDecoder")) return new Error("This model is meshopt-compressed. Previewer's viewer does not open it.");
  return new Error(`The model could not be read: ${message}`);
}

// The 3MF loader ignores the model's unit attribute, so read it from the package.
function threeMfUnit(buffer) {
  try {
    const files = unzipSync(new Uint8Array(buffer), { filter: (file) => /\.model$/i.test(file.name) });
    const first = Object.values(files)[0];
    const head = new TextDecoder().decode(first.subarray(0, 4096));
    const unit = /<model\b[^>]*\bunit\s*=\s*"([a-z]+)"/i.exec(head)?.[1]?.toLowerCase();
    return THREE_MF_MM[unit] || 1;
  } catch { return 1; }
}

function unitNote(kind, mmPerUnit) {
  if (kind === "glb" || kind === "gltf") return "glTF sizes are in metres; shown here in millimetres.";
  if (kind === "3mf") return mmPerUnit === 1 ? "3MF unit: millimetres." : `3MF unit converted to millimetres (×${mmPerUnit}).`;
  return `${kind.toUpperCase()} files name no unit; read as millimetres, as slicers do.`;
}

function niceStep(value) {
  if (!(value > 0)) return 1;
  const power = 10 ** Math.floor(Math.log10(value));
  // Float32 vertices put a 50 mm part at 50.000001 mm; that still gets 10 mm squares, not 20.
  return [1, 2, 5, 10].map((step) => step * power).find((step) => step >= value * 0.999) || 10 * power;
}

function round(value) { return Math.round(value * 1e6) / 1e6; }
