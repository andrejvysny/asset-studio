@tool
extends RefCounted
# Single source of truth for the addon version; scripts/package_addon.py reads VERSION from this file.

const VERSION: String = "0.1.0"
const CONTRACT_VERSION: int = 1
const API_VERSION: int = 1
