from datetime import datetime
import os
import re
import time
import unicodedata
from dotenv import load_dotenv
from flask import Flask, abort, render_template, request
import requests

load_dotenv()

app = Flask(__name__)

# --- CẤU HÌNH NOTION (CÓ DỰ PHÒNG TRỰC TIẾP TRÁNH LỖI THIẾU .ENV) ---
NOTION_TOKEN = os.environ.get(
    "NOTION_TOKEN"
) or "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"
NOTION_DATABASE_ID = (
    os.environ.get("NOTION_DATABASE_ID") or "29c4cc55b91180c29e49fa2517db22b7"
)

# Mapping tên trường từ Notion Database của bạn
P_NAMES = {
    "TITLE_RO": "Tên Romanji",
    "TITLE_EN": "Tên tiếng Anh",
    "SERIES": "Loạt phim",
    "STATUS": "Trạng thái",
    "YEAR": "Năm",
    "GENRES": "Thể loại",
    "POSTER": "Ảnh",
    "SUMMARY": "Tóm tắt nội dung",
    "NOTE": "Ghi chú",
    "SUB_URL": "Tải xuống phụ đề",
    "RAW_URL": "Tải xuống raw",
    "FANSUB": "Bản quyền/Nhóm dịch",
    "RAW_TYPE": "Loại Raw",
    "EPISODES": "Số tập",
    "EPISODES_SUB": "Số tập Vietsub",
    "TYPE": "Loại",
    "UPDATE_DATE": "Ngày cập nhật",
    "IS_PUBLIC": "Public",
}

# --- CACHE (Bộ nhớ đệm 10 phút) ---
DATA_CACHE = None
LAST_FETCH_TIME = 0
CACHE_DURATION = int(os.environ.get("CACHE_DURATION", 600))


def slugify(value):
    if not value:
        return "anime"
    value = str(value)
    value = (
        unicodedata.normalize("NFKD", value)
        .encode("ascii", "ignore")
        .decode("ascii")
    )
    value = re.sub(r"[^\w\s-]", "", value.lower())
    return re.sub(r"[-\s]+", "-", value).strip("-")


def get_notion_data():
    """Lấy dữ liệu từ Notion API với phân trang an toàn"""
    if not NOTION_TOKEN or not NOTION_DATABASE_ID:
        print("❌ [DEBUG] LỖI: Chưa có NOTION_TOKEN hoặc NOTION_DATABASE_ID!")
        return []

    headers = {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Content-Type": "application/json",
        "Notion-Version": "2022-06-28",
    }
    url = f"https://api.notion.com/v1/databases/{NOTION_DATABASE_ID}/query"

    all_results = []
    has_more = True
    next_cursor = None

    try:
        while has_more:
            payload = {"page_size": 100}
            if next_cursor:
                payload["start_cursor"] = next_cursor

            response = requests.post(
                url, json=payload, headers=headers, timeout=25
            )

            if response.status_code != 200:
                print(
                    f"❌ [DEBUG] Notion API trả về mã lỗi {response.status_code}:"
                )
                print(response.text)
                return []

            data = response.json()
            all_results.extend(data.get("results", []))
            has_more = data.get("has_more", False)
            next_cursor = data.get("next_cursor")

        print(
            f"✅ [DEBUG] Kết nối Notion thành công! Tải về được {len(all_results)} dòng dữ liệu thô."
        )
        return all_results
    except Exception as e:
        print(f"❌ [DEBUG] Lỗi kết nối Notion: {e}")
        return []


def process_data(raw_data):
    """Xử lý dữ liệu thô an toàn (chống sập khi có dòng trống)"""
    cleaned_data = []
    for p in raw_data:
        props = p.get("properties", {})

        def get_prop(k):
            if k not in P_NAMES:
                return ""
            d = props.get(P_NAMES[k], {})
            ptype = d.get("type")

            # Xử lý title an toàn (không bị IndexError khi rỗng)
            if ptype == "title":
                items = d.get("title", [])
                return items[0].get("plain_text", "") if items else ""

            # Xử lý rich_text an toàn
            if ptype == "rich_text":
                items = d.get("rich_text", [])
                return items[0].get("plain_text", "") if items else ""

            if ptype == "select":
                return (d.get("select") or {}).get("name", "")
            if ptype == "status":
                return (d.get("status") or {}).get("name", "")
            if ptype == "number":
                return str(d.get("number") or 0)
            if ptype == "multi_select":
                return ", ".join([o["name"] for o in d.get("multi_select", [])])
            if ptype == "url":
                return d.get("url") or ""
            return ""

        item = {k.lower(): get_prop(k) for k in P_NAMES.keys()}

        # Loại Raw
        raw_type_prop = props.get(P_NAMES["RAW_TYPE"], {})
        item["raw_type"] = (
            raw_type_prop.get("status", {}).get("name")
            or raw_type_prop.get("select", {}).get("name")
            or get_prop("RAW_TYPE")
        )

        # Link Sub & Raw (hỗ trợ cả kiểu URL lẫn Rich Text)
        sub_d = props.get(P_NAMES["SUB_URL"], {})
        item["sub_url"] = (
            (sub_d.get("url") or get_prop("SUB_URL") or "").strip()
        )

        raw_d = props.get(P_NAMES["RAW_URL"], {})
        item["raw_url"] = (
            (raw_d.get("url") or get_prop("RAW_URL") or "").strip()
        )

        # Checkbox Public
        is_public_prop = props.get(P_NAMES["IS_PUBLIC"], {})
        item["is_public"] = is_public_prop.get("checkbox", False)

        # Số tập Vietsub
        item["episodes_sub"] = get_prop("EPISODES_SUB") or item.get(
            "episodes", "0"
        )

        # Ngày cập nhật
        date_prop = props.get(P_NAMES["UPDATE_DATE"], {}).get("date")
        if date_prop and isinstance(date_prop, dict) and date_prop.get("start"):
            item["last_edited"] = date_prop.get("start")
        else:
            item["last_edited"] = p.get("last_edited_time", "")

        # Poster ảnh
        f = props.get(P_NAMES["POSTER"], {}).get("files", [])
        if f:
            item["poster_url"] = (
                f[0]["external"]["url"]
                if "external" in f[0]
                else f[0].get("file", {}).get("url", "")
            )
        else:
            item["poster_url"] = (
                "https://images.unsplash.com/photo-1578632767115-351597cf2477?w=600&auto=format&fit=crop&q=80"
            )

        title_for_slug = item["title_ro"] or item["title_en"] or "anime"
        item["slug"] = f"{slugify(title_for_slug)}-{p.get('id', '')[-4:]}"

        cleaned_data.append(item)

    return cleaned_data


def get_data(force=False):
    global DATA_CACHE, LAST_FETCH_TIME
    current_time = time.time()

    if not force and DATA_CACHE and (current_time - LAST_FETCH_TIME < CACHE_DURATION):
        return DATA_CACHE

    raw_notion = get_notion_data()
    if raw_notion:
        clean_data = process_data(raw_notion)
        DATA_CACHE = clean_data
        LAST_FETCH_TIME = current_time

        # --- LOG CHẨN ĐOÁN DỮ LIỆU ---
        total_sub = sum(1 for x in clean_data if x.get("sub_url"))
        total_public = sum(1 for x in clean_data if x.get("is_public"))
        total_valid = sum(
            1 for x in clean_data if x.get("sub_url") and x.get("is_public")
        )
        print(f"📊 [DEBUG THỐNG KÊ]:")
        print(f"   • Tổng số phim đọc được: {len(clean_data)}")
        print(f"   • Số phim CÓ LINK SUB: {total_sub}")
        print(f"   • Số phim ĐÃ TÍCH PUBLIC: {total_public}")
        print(f"   • Số phim ĐỦ ĐIỀU KIỆN HIỂN THỊ (Sub + Public): {total_valid}")

        if total_valid == 0:
            print(
                "⚠️ [CẢNH BÁO] Không có phim nào hiển thị vì không có phim nào đồng thời có Sub VÀ được tích chọn Public!"
            )

        return clean_data

    return DATA_CACHE if DATA_CACHE else []


# --- ROUTES ---


@app.route("/")
def home():
    data = get_data()

    # Điều kiện: Có link sub VÀ đã check Public
    valid_data = [x for x in data if x.get("sub_url") and x.get("is_public")]
    valid_data.sort(key=lambda x: x.get("last_edited", ""), reverse=True)

    # Lấy 30 phim mới nhất
    latest_30 = valid_data[:30]
    slider_data = latest_30[:5]

    return render_template(
        "home.html", slider_data=slider_data, latest_anime=latest_30
    )


@app.route("/list")
def anime_list():
    data = get_data()

    # Điều kiện: Có link sub VÀ đã check Public
    valid_data = [x for x in data if x.get("sub_url") and x.get("is_public")]
    valid_data.sort(key=lambda x: (x.get("title_ro") or "").lower())

    raw_seasons = list(set([x["year"] for x in valid_data if x.get("year")]))

    def season_sorter(season_str):
        parts = season_str.strip().split()
        if len(parts) < 2:
            return (0, 0)
        year = int(parts[1]) if parts[1].isdigit() else 0
        order = {"Winter": 1, "Spring": 2, "Summer": 3, "Fall": 4}
        return (year, order.get(parts[0].capitalize(), 0))

    seasons = sorted(raw_seasons, key=season_sorter, reverse=True)
    return render_template("list.html", anime_list=valid_data, seasons=seasons)


@app.route("/chat")
def chat():
    return render_template("chat.html")


@app.route("/anime/<slug>")
def anime_detail(slug):
    data = get_data()
    anime = next((item for item in data if item["slug"] == slug), None)

    if not anime or not anime.get("is_public"):
        abort(404)

    related = []
    if anime.get("series") and anime["series"] not in ["", "Không rõ", "None"]:
        related = [
            x
            for x in data
            if x.get("series") == anime["series"]
            and x["slug"] != slug
            and x.get("is_public")
            and x.get("sub_url")
        ]

    return render_template("detail.html", anime=anime, related=related)


@app.route("/refresh-cache")
def refresh_cache():
    get_data(force=True)
    return {
        "status": "success",
        "message": "Đã làm mới dữ liệu từ Notion thành công!",
    }


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)