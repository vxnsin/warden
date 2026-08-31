from warden import theme


def test_the_mascot_is_a_rectangle():
    assert {len(line) for line in theme.MASCOT} == {17}


def test_the_wordmark_is_a_rectangle():
    assert {len(line) for line in theme.WORDMARK} == {51}


def test_the_banner_rows_all_have_the_same_width():
    assert len({len(line) for line in theme.BANNER.splitlines()}) == 1


def test_the_banner_is_as_tall_as_the_mascot():
    assert len(theme.BANNER.splitlines()) == len(theme.MASCOT)


def test_the_wordmark_sits_beside_the_mascot_not_above_it():
    middle = theme.BANNER.splitlines()[len(theme.MASCOT) // 2]
    width = len(theme.MASCOT[0])
    assert middle[:width].strip()
    assert middle[width + len(theme.GAP) :].strip()


def test_the_banner_keeps_its_blocks_on_a_utf8_console():
    assert theme.BLOCK in theme.banner_for("utf-8")


def test_the_banner_falls_back_where_blocks_cannot_be_printed():
    fallback = theme.banner_for("cp1252")
    assert theme.BLOCK not in fallback
    assert "#" in fallback


def test_the_heart_is_never_left_as_a_bare_asterisk():
    assert theme.HEART not in theme.banner_for("utf-8")
    assert theme.HEART not in theme.banner_for("cp1252")


def test_an_unknown_encoding_does_not_crash_the_banner():
    assert theme.banner_for("not-a-real-codec")


def test_the_heart_is_the_only_lit_part_of_the_mascot():
    text = theme.banner_text("utf-8")
    lit = {span.style for span in text.spans}
    assert theme.GLOW in lit
    assert theme.GLOW_DIM in lit


def test_the_styled_banner_says_the_same_as_the_plain_one():
    assert theme.banner_text("utf-8").plain == theme.banner_for("utf-8")
    assert theme.banner_text("cp1252").plain == theme.banner_for("cp1252")


def test_each_kind_of_service_gets_its_own_colour():
    assert theme.kind_colour("backend") == theme.GLOW
    assert theme.kind_colour("frontend") == theme.AMETHYST
    assert theme.kind_colour("anything-else") == theme.BONE_DIM
