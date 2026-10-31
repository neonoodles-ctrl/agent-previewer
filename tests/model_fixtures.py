"""Small 3D files built in code for the 3D viewer tests: cubes as GLB, STL and 3MF."""
from __future__ import annotations

import io
import json
import struct
import zipfile

from PIL import Image

# Each face: its normal and two in-plane axes u, v with u x v = normal, so corners run counter-clockwise outside.
FACES = [
    ((1, 0, 0), (0, 1, 0), (0, 0, 1)), ((-1, 0, 0), (0, 0, 1), (0, 1, 0)),
    ((0, 1, 0), (0, 0, 1), (1, 0, 0)), ((0, -1, 0), (1, 0, 0), (0, 0, 1)),
    ((0, 0, 1), (1, 0, 0), (0, 1, 0)), ((0, 0, -1), (0, 1, 0), (1, 0, 0)),
]


def cube_faces(size: float, drop_faces: int = 0) -> list[list[tuple[float, float, float]]]:
    """Four corners per face of a cube centred on the origin, minus the last drop_faces faces."""
    half = size / 2
    faces = []
    for normal, u, v in FACES[: len(FACES) - drop_faces]:
        centre = [axis * half for axis in normal]
        corners = []
        for su, sv in ((-1, -1), (1, -1), (1, 1), (-1, 1)):
            corners.append(tuple(centre[i] + su * half * u[i] + sv * half * v[i] for i in range(3)))
        faces.append(corners)
    return faces


def cube_triangles(size: float, drop_faces: int = 0, offset: tuple[float, float, float] = (0, 0, 0)) -> list[tuple]:
    triangles = []
    for a, b, c, d in cube_faces(size, drop_faces):
        moved = [tuple(p[i] + offset[i] for i in range(3)) for p in (a, b, c, d)]
        triangles += [(moved[0], moved[1], moved[2]), (moved[0], moved[2], moved[3])]
    return triangles


def binary_stl(triangles: list[tuple]) -> bytes:
    body = b"".join(struct.pack("<12fH", 0, 0, 0, *a, *b, *c, 0) for a, b, c in triangles)
    return b"previewer test stl".ljust(80, b" ") + struct.pack("<I", len(triangles)) + body


def three_mf(triangles: list[tuple], unit: str = "millimeter") -> bytes:
    vertices: dict[tuple, int] = {}
    indexed = []
    for triangle in triangles:
        indexed.append([vertices.setdefault(point, len(vertices)) for point in triangle])
    vertex_xml = "".join(f'<vertex x="{x}" y="{y}" z="{z}"/>' for (x, y, z) in vertices)
    triangle_xml = "".join(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in indexed)
    model = (
        f'<?xml version="1.0" encoding="UTF-8"?><model unit="{unit}" xml:lang="en-US" '
        'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02"><resources><object id="1" type="model"><mesh>'
        f"<vertices>{vertex_xml}</vertices><triangles>{triangle_xml}</triangles>"
        '</mesh></object></resources><build><item objectid="1"/></build></model>'
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/></Types>')
        archive.writestr("_rels/.rels", '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Target="/3D/3dmodel.model" Id="rel0" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/></Relationships>')
        archive.writestr("3D/3dmodel.model", model)
    return buffer.getvalue()


def checker_png(size: int = 8) -> bytes:
    image = Image.new("RGB", (size, size))
    pixels = image.load()
    for y in range(size):
        for x in range(size):
            pixels[x, y] = (230, 40, 40) if (x // 2 + y // 2) % 2 else (40, 80, 230)
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


def cube_glb(size: float = 0.05, texture: bytes | None = None, animate: bool = False) -> bytes:
    """A cube in metres with split vertices per face, as exporters write them; optionally textured and spinning."""
    positions, normals, uvs, indices = [], [], [], []
    for (normal, _, _), corners in zip(FACES, cube_faces(size)):
        base = len(positions)
        positions += corners
        normals += [normal] * 4
        uvs += [(0, 1), (1, 1), (1, 0), (0, 0)]
        indices += [base, base + 1, base + 2, base, base + 2, base + 3]
    chunks: list[bytes] = []
    views = []

    def add(data: bytes) -> int:
        offset = sum(len(chunk) for chunk in chunks)
        chunks.append(data + b"\0" * (-len(data) % 4))
        views.append({"buffer": 0, "byteOffset": offset, "byteLength": len(data)})
        return len(views) - 1

    flat = lambda rows: [value for row in rows for value in row]
    accessors = [
        {"bufferView": add(struct.pack(f"<{len(positions) * 3}f", *flat(positions))), "componentType": 5126, "count": len(positions),
         "type": "VEC3", "min": [-size / 2] * 3, "max": [size / 2] * 3},
        {"bufferView": add(struct.pack(f"<{len(normals) * 3}f", *flat(normals))), "componentType": 5126, "count": len(normals), "type": "VEC3"},
        {"bufferView": add(struct.pack(f"<{len(uvs) * 2}f", *flat(uvs))), "componentType": 5126, "count": len(uvs), "type": "VEC2"},
        {"bufferView": add(struct.pack(f"<{len(indices)}H", *indices)), "componentType": 5123, "count": len(indices), "type": "SCALAR"},
    ]
    material = {"pbrMetallicRoughness": {"baseColorFactor": [0.8, 0.8, 0.8, 1], "metallicFactor": 0, "roughnessFactor": 0.8}}
    document = {
        "asset": {"version": "2.0"},
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": [{"mesh": 0, "name": "cube"}],
        "meshes": [{"primitives": [{"attributes": {"POSITION": 0, "NORMAL": 1, "TEXCOORD_0": 2}, "indices": 3, "material": 0}]}],
        "materials": [material],
        "accessors": accessors,
    }
    if texture is not None:
        material["pbrMetallicRoughness"]["baseColorTexture"] = {"index": 0}
        material["pbrMetallicRoughness"]["baseColorFactor"] = [1, 1, 1, 1]
        document["textures"] = [{"source": 0}]
        document["images"] = [{"bufferView": add(texture), "mimeType": "image/png"}]
    if animate:
        times = [0.0, 1.0, 2.0]
        turns = [(0, 0, 0, 1), (0, 0.7071068, 0, 0.7071068), (0, 1, 0, 0)]
        accessors.append({"bufferView": add(struct.pack("<3f", *times)), "componentType": 5126, "count": 3, "type": "SCALAR", "min": [0], "max": [2]})
        accessors.append({"bufferView": add(struct.pack("<12f", *flat(turns))), "componentType": 5126, "count": 3, "type": "VEC4"})
        document["animations"] = [{"name": "spin", "samplers": [{"input": len(accessors) - 2, "output": len(accessors) - 1}],
                                   "channels": [{"sampler": 0, "target": {"node": 0, "path": "rotation"}}]}]
    binary = b"".join(chunks)
    document["buffers"] = [{"byteLength": len(binary)}]
    document["bufferViews"] = views
    return pack_glb(document, binary)


def draco_glb() -> bytes:
    """A GLB that requires Draco, which Previewer's viewer does not decode."""
    document = {
        "asset": {"version": "2.0"},
        "extensionsUsed": ["KHR_draco_mesh_compression"],
        "extensionsRequired": ["KHR_draco_mesh_compression"],
        "scene": 0, "scenes": [{"nodes": [0]}], "nodes": [{"mesh": 0}],
        "meshes": [{"primitives": [{"attributes": {"POSITION": 0}, "extensions": {"KHR_draco_mesh_compression": {"bufferView": 0, "attributes": {"POSITION": 0}}}}]}],
        "accessors": [{"componentType": 5126, "count": 3, "type": "VEC3", "min": [0, 0, 0], "max": [1, 1, 0]}],
        "bufferViews": [{"buffer": 0, "byteOffset": 0, "byteLength": 4}],
        "buffers": [{"byteLength": 4}],
    }
    return pack_glb(document, b"\0\0\0\0")


def pack_glb(document: dict, binary: bytes) -> bytes:
    text = json.dumps(document, separators=(",", ":")).encode()
    text += b" " * (-len(text) % 4)
    binary += b"\0" * (-len(binary) % 4)
    total = 12 + 8 + len(text) + 8 + len(binary)
    return (struct.pack("<4sII", b"glTF", 2, total) + struct.pack("<I4s", len(text), b"JSON") + text
            + struct.pack("<I4s", len(binary), b"BIN\0") + binary)
