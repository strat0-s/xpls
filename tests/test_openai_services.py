from didac_app.openai_services import json_to_lead_text, openai_enabled, split_lead_texts


def test_json_to_lead_text_maps_vision_output_to_lead_fields():
    text = json_to_lead_text(
        '{"first_name":"Priya","last_name":"Rao","org":"ABC Foundation","designation":"CSR Lead","phone":"9876543210","interest":"science labs","pincode":"560001","udise_code":"29040100101"}'
    )
    assert "First Name: Priya" in text
    assert "Last Name: Rao" in text
    assert "Org: ABC Foundation" in text
    assert "Designation: CSR Lead" in text
    assert "Phone: 9876543210" in text
    assert "Interest: science labs" in text
    assert "Pincode: 560001" in text
    assert "UDISE Code: 29040100101" in text


def test_json_to_lead_text_maps_qr_fields():
    text = json_to_lead_text(
        '{"name":"Nisha Rao","org":"DIDAC Visitor","qr_url":"https://event.example/profile/123","qr_page_text":"Nisha Rao Principal Bengaluru"}'
    )
    assert "QR URL: https://event.example/profile/123" in text
    assert "QR Page Text: Nisha Rao Principal Bengaluru" in text


def test_openai_enabled_is_boolean():
    assert isinstance(openai_enabled(), bool)


def test_split_lead_texts_handles_multiple_contacts():
    texts = split_lead_texts(
        '{"contacts":[{"first_name":"Priya","org":"ABC Foundation"},{"first_name":"Ramesh","org":"XYZ School"}]}'
    )
    assert len(texts) == 2
    assert "Org: ABC Foundation" in texts[0]
    assert "Org: XYZ School" in texts[1]


def test_split_lead_texts_handles_contact_separator():
    texts = split_lead_texts("First Name: Priya\nOrg: ABC\n\n---CONTACT---\n\nFirst Name: Ramesh\nOrg: XYZ")
    assert len(texts) == 2
    assert "Org: ABC" in texts[0]
    assert "Org: XYZ" in texts[1]


def test_big_photos_are_shrunk_upright_and_small_ones_are_left_alone(tmp_path):
    import struct

    import cv2
    import numpy as np

    from didac_app.openai_services import shrunk_jpeg

    big = tmp_path / "big.jpg"
    data = cv2.imencode(".jpg", np.zeros((1500, 3000, 3), np.uint8))[1].tobytes()
    exif = b"MM\x00*\x00\x00\x00\x08\x00\x01\x01\x12\x00\x03\x00\x00\x00\x01\x00\x06\x00\x00\x00\x00\x00\x00"  # EXIF Orientation = 6 (turn right)
    big.write_bytes(data[:2] + b"\xff\xe1" + struct.pack(">H", len(exif) + 8) + b"Exif\x00\x00" + exif + data[2:])
    small = tmp_path / "small.jpg"
    small.write_bytes(cv2.imencode(".jpg", np.zeros((800, 600, 3), np.uint8))[1].tobytes())

    out = cv2.imdecode(np.frombuffer(shrunk_jpeg(str(big)), np.uint8), cv2.IMREAD_COLOR)
    assert out.shape[:2] == (2000, 1000)  # turned upright (portrait) and longest side 2000
    assert shrunk_jpeg(str(small)) is None
    assert shrunk_jpeg(str(tmp_path / "missing.jpg")) is None
