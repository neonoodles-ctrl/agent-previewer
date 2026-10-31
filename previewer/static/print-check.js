// Print check for Previewer's 3D viewer, run off the page's main thread.
// Input: world positions in millimetres and triangle indices. Vertices are welded by position first,
// because exporters split them at texture seams and a seam is not a hole.
self.onmessage = ({ data }) => { self.postMessage(check(data.positions, data.indices)); };

function check(positions, indices) {
  const count = positions.length / 3;
  let minX = Infinity, minY = Infinity, minZ = Infinity, maxX = -Infinity, maxY = -Infinity, maxZ = -Infinity;
  for (let index = 0; index < count; index += 1) {
    const x = positions[index * 3], y = positions[index * 3 + 1], z = positions[index * 3 + 2];
    if (x < minX) minX = x; if (x > maxX) maxX = x;
    if (y < minY) minY = y; if (y > maxY) maxY = y;
    if (z < minZ) minZ = z; if (z > maxZ) maxZ = z;
  }
  // 2^17 steps across the longest side keeps the packed key under 2^53, so it stays an exact number.
  const steps = 131072;
  const cell = Math.max(maxX - minX, maxY - minY, maxZ - minZ) / steps || 1;
  const weld = new Int32Array(count);
  const seen = new Map();
  for (let index = 0; index < count; index += 1) {
    const qx = Math.round((positions[index * 3] - minX) / cell);
    const qy = Math.round((positions[index * 3 + 1] - minY) / cell);
    const qz = Math.round((positions[index * 3 + 2] - minZ) / cell);
    const key = (qx * (steps + 1) + qy) * (steps + 1) + qz;
    let id = seen.get(key);
    if (id === undefined) { id = seen.size; seen.set(key, id); }
    weld[index] = id;
  }
  const welded = seen.size;
  const triangles = indices.length / 3;
  const edges = new Float64Array(triangles * 3);
  const parent = new Int32Array(welded);
  for (let index = 0; index < welded; index += 1) parent[index] = index;
  const used = new Uint8Array(welded);
  const find = (value) => {
    while (parent[value] !== value) { parent[value] = parent[parent[value]]; value = parent[value]; }
    return value;
  };
  const join = (left, right) => { const a = find(left), b = find(right); if (a !== b) parent[a] = b; };
  let edgeCount = 0;
  let volume = 0;
  for (let triangle = 0; triangle < triangles; triangle += 1) {
    const ia = indices[triangle * 3], ib = indices[triangle * 3 + 1], ic = indices[triangle * 3 + 2];
    const a = weld[ia], b = weld[ib], c = weld[ic];
    if (a === b || b === c || a === c) continue;
    edges[edgeCount++] = a < b ? a * welded + b : b * welded + a;
    edges[edgeCount++] = b < c ? b * welded + c : c * welded + b;
    edges[edgeCount++] = c < a ? c * welded + a : a * welded + c;
    join(a, b);
    join(b, c);
    used[a] = used[b] = used[c] = 1;
    // Signed volume of the tetrahedron from the box corner to the triangle; the sum is the enclosed volume.
    const ax = positions[ia * 3] - minX, ay = positions[ia * 3 + 1] - minY, az = positions[ia * 3 + 2] - minZ;
    const bx = positions[ib * 3] - minX, by = positions[ib * 3 + 1] - minY, bz = positions[ib * 3 + 2] - minZ;
    const cx = positions[ic * 3] - minX, cy = positions[ic * 3 + 1] - minY, cz = positions[ic * 3 + 2] - minZ;
    volume += (ax * (by * cz - bz * cy) - ay * (bx * cz - bz * cx) + az * (bx * cy - by * cx)) / 6;
  }
  const sorted = edges.subarray(0, edgeCount).sort();
  let openEdges = 0;
  let nonManifoldEdges = 0;
  for (let start = 0; start < edgeCount;) {
    let end = start + 1;
    while (end < edgeCount && sorted[end] === sorted[start]) end += 1;
    if (end - start === 1) openEdges += 1;
    else if (end - start > 2) nonManifoldEdges += 1;
    start = end;
  }
  let pieces = 0;
  for (let index = 0; index < welded; index += 1) if (used[index] && find(index) === index) pieces += 1;
  return { triangles, openEdges, nonManifoldEdges, pieces, watertight: openEdges === 0 && nonManifoldEdges === 0, volumeMm3: Math.abs(volume) };
}
