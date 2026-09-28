import pytest
from jobcore.prompting import compose_effective, split_template

T = "single isolated object, plain uniform light gray background, no floor"


def test_template_appended_once() -> None:
    assert compose_effective("a wooden barrel", T) == f"a wooden barrel, {T}"


def test_user_cannot_remove_template() -> None:
    assert compose_effective("a crate on a table", T).endswith(T)


def test_template_not_duplicated_if_user_kept_it() -> None:
    eff = compose_effective(f"a barrel, {T}", T)
    assert eff.count(T) == 1


def test_split_template_mid_text() -> None:
    assert split_template(f"a barrel, {T}, stylized", T) == "a barrel, stylized"


def test_empty_description_rejected() -> None:
    with pytest.raises(ValueError):
        compose_effective(f"  {T}  ", T)
