shader_type spatial;
#include "res://tests/data/publish/crystal.gdshaderinc"

uniform vec4 tint : source_color = vec4(0.4, 0.8, 1.0, 1.0);

void fragment() {
	ALBEDO = tint.rgb;
	EMISSION = fresnel_glow(NORMAL, VIEW, 3.0) * tint.rgb;
}
