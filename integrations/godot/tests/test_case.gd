extends RefCounted

var failures: PackedStringArray = PackedStringArray()


func fail(msg: String) -> void:
	failures.append(msg)


func assert_true(cond: bool, msg: String = "") -> void:
	if not cond:
		fail("assert_true failed: %s" % msg)


func assert_eq(a: Variant, b: Variant, msg: String = "") -> void:
	if typeof(a) != typeof(b) or a != b:
		fail("assert_eq failed: %s != %s %s" % [str(a), str(b), msg])
