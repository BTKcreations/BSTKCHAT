import pytest
from bstkchat.ui import get_user_color, generate_room_id

def test_get_user_color():
    color1 = get_user_color("Alice")
    color2 = get_user_color("Alice")
    assert color1 == color2

    # Not guaranteed to be different due to collisions, but generally should test it returns a string
    assert isinstance(color1, str)

def test_generate_room_id():
    room1 = generate_room_id()
    room2 = generate_room_id()
    assert room1 != room2
    assert len(room1.split("-")) == 3
