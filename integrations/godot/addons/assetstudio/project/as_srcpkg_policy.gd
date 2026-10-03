@tool
extends RefCounted
# The single policy module for the static source package grammar, shared by the installer (as_srcpkg_*) and the
# publisher (as_source_*, as_glb_inspect): allowlists and limits of static-source-package.md and capabilities.json
# (`source_package`, `limits`). A test compares these constants with the contract file.

const REPRESENTATION: String = "godot_static_source_v1"
const MANIFEST_NAME: String = "source_manifest.json"
const ARCHIVE_NAME: String = "source.zip"

const MAX_FILES: int = 4096
const MAX_DEPTH: int = 32
const MAX_EXPANDED_BYTES: int = 1073741824
const MAX_UPLOAD_BYTES: int = 536870912
const MAX_SLOTS: int = 64
const MAX_SURFACES_PER_SLOT: int = 1024
const IPAD_MAX_TRIANGLES: int = 200000
const IPAD_MAX_NODES: int = 1024
const IPAD_MAX_MATERIALS: int = 64
const IPAD_MAX_TEXTURE_PX: int = 4096
const IPAD_GLB_MAX_BYTES: int = 134217728
const MAX_MANIFEST_BYTES: int = 8 * 1024 * 1024
const RATIO_LIMIT: int = 200
const RATIO_MIN_BYTES: int = 1048576

const TEXT_EXTENSIONS: PackedStringArray = [".tscn", ".tres"]
const SHADER_EXTENSIONS: PackedStringArray = [".gdshader", ".gdshaderinc"]
const MEDIA: Dictionary = {".tscn": "text/x-godot-scene", ".tres": "text/x-godot-resource",
		".gdshader": "text/x-godot-shader", ".gdshaderinc": "text/x-godot-shader", ".png": "image/png",
		".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp", ".glb": "model/gltf-binary"}
const ALLOWED_EXTENSIONS: PackedStringArray = [".tscn", ".tres", ".gdshader", ".gdshaderinc", ".png", ".jpg", ".jpeg",
		".webp", ".glb"]
const FORBIDDEN_EXTENSIONS: PackedStringArray = [".gd", ".cs", ".gdextension", ".pck", ".so", ".dylib", ".dll", ".exe",
		".res", ".scn", "project.godot"]
const ALLOWED_NODE_TYPES: PackedStringArray = ["Node3D", "MeshInstance3D", "CSGBox3D", "CSGCylinder3D", "CSGSphere3D",
		"CSGTorus3D", "CSGPolygon3D", "CSGMesh3D", "CSGCombiner3D", "StaticBody3D", "CollisionShape3D", "Marker3D"]
const ALLOWED_RESOURCE_TYPES: PackedStringArray = ["StandardMaterial3D", "ORMMaterial3D", "ShaderMaterial", "Shader",
		"ArrayMesh", "BoxMesh", "CylinderMesh", "SphereMesh", "PlaneMesh", "QuadMesh", "PrismMesh", "CapsuleMesh",
		"TorusMesh", "BoxShape3D", "SphereShape3D", "CapsuleShape3D", "CylinderShape3D", "ConvexPolygonShape3D",
		"ConcavePolygonShape3D", "Texture2D", "CompressedTexture2D", "PackedScene"]
## Engine save class of an external material in a scene ([ext_resource type="Material"]); the .tres header is checked itself.
const EXT_SAVE_CLASSES: PackedStringArray = ["Material"]
const KNOWN_CAPABILITIES: PackedStringArray = ["godot_text_scene_v1", "csg_static", "static_collision", "shader_source",
		"vertex_colors", "alpha_mask", "alpha_blend", "pbr_textures"]

## Classes inside an opaque (.glb) instance that make the scene non-static (blocked, never dropped).
const NON_STATIC_CLASSES: PackedStringArray = ["AnimationPlayer", "AnimationTree", "Skeleton3D", "BoneAttachment3D",
		"GPUParticles3D", "CPUParticles3D"]
const SCRIPT_TYPES: PackedStringArray = ["Script", "GDScript", "CSharpScript", "GDExtension"]
const SECTION_KINDS: PackedStringArray = ["ext_resource", "sub_resource", "node", "resource", "editable"]
## Value constructors that are inert data (never a class instantiation).
const CONSTRUCTORS: PackedStringArray = ["Vector2", "Vector2i", "Vector3", "Vector3i", "Vector4", "Vector4i", "Color",
		"Transform2D", "Transform3D", "Basis", "Quaternion", "AABB", "Plane", "Rect2", "Rect2i", "Projection",
		"StringName", "NodePath", "Array", "Dictionary", "PackedByteArray", "PackedInt32Array", "PackedInt64Array",
		"PackedFloat32Array", "PackedFloat64Array", "PackedStringArray", "PackedVector2Array", "PackedVector3Array",
		"PackedVector4Array", "PackedColorArray"]
## glTF extensions a self-contained packaged .glb may require.
const ALLOWED_REQUIRED_EXTENSIONS: PackedStringArray = ["KHR_texture_transform", "KHR_materials_emissive_strength",
		"KHR_materials_unlit"]

## Publisher defaults of a draft descriptor's placement.
const DEFAULT_SCALE_RANGE: Array = ["0.5", "2"]
const DEFAULT_HEIGHT_RANGE: Array = ["-0.1", "0.5"]
const DEFAULT_GROUNDING: String = "FOLLOW_TERRAIN"
