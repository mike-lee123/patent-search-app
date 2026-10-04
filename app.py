import streamlit as st
import streamlit.components.v1 as components
import pandas as pd
import json
import os
import io
import re
import time
import urllib.parse
import requests
from bs4 import BeautifulSoup
from datetime import datetime
from dataclasses import dataclass, field
from typing import List, Dict
from PIL import Image, ImageDraw, ImageFont

# 引入 Google GenAI SDK
try:
    from google import genai
    from google.genai import types
    HAS_GENAI = True
except ImportError:
    HAS_GENAI = False

# ==============================================================================
# 一、 核心資料結構與專利檢索邏輯
# ==============================================================================
@dataclass
class TechnicalPillar:
    """定義單一技術支柱（名稱、英文關鍵字、中文關鍵字）"""
    name: str
    en_keywords: List[str] = field(default_factory=list)
    zh_keywords: List[str] = field(default_factory=list)

class PatentSearchBuilder:
    """專利檢索邏輯式建造器"""
    def __init__(self, target_title: str):
        self.target_title = target_title
        self.ipc_classes: List[str] = []
        self.cpc_classes: List[str] = []
        self.pillars: List[TechnicalPillar] = []

    def add_ipc(self, *ipc_codes: str) -> "PatentSearchBuilder":
        self.ipc_classes.extend([code.strip() for code in ipc_codes if code.strip()])
        return self

    def add_cpc(self, *cpc_codes: str) -> "PatentSearchBuilder":
        self.cpc_classes.extend([code.strip() for code in cpc_codes if code.strip()])
        return self

    def add_pillar(self, name: str, en_keywords: List[str], zh_keywords: List[str]) -> "PatentSearchBuilder":
        self.pillars.append(
            TechnicalPillar(
                name=name,
                en_keywords=[kw.strip() for kw in en_keywords if kw.strip()],
                zh_keywords=[kw.strip() for kw in zh_keywords if kw.strip()]
            )
        )
        return self

    def to_google_patents_query(self) -> str:
        pillar_blocks = []
        for p in self.pillars:
            if p.en_keywords:
                formatted = [f'"{kw}"' if " " in kw else kw for kw in p.en_keywords]
                pillar_blocks.append(f"({' OR '.join(formatted)})")

        keyword_part = " AND ".join(pillar_blocks) if pillar_blocks else ""
        all_classes = self.cpc_classes or self.ipc_classes
        if all_classes:
            classes_str = " OR ".join(all_classes)
            if keyword_part:
                return f"({keyword_part}) AND ({classes_str})"
            return f"({classes_str})"
        return keyword_part

    def to_gpss_query(self, search_fields: str = "TI,AB,CL") -> str:
        pillar_blocks = []
        for p in self.pillars:
            all_kw = p.zh_keywords + p.en_keywords
            if all_kw:
                formatted = [f'"{kw}"' if " " in kw else kw for kw in all_kw]
                pillar_blocks.append(f"({' OR '.join(formatted)})")

        query_body = " AND ".join(pillar_blocks) if pillar_blocks else ""
        formatted_query = f"{search_fields}=({query_body})" if query_body else ""

        if self.ipc_classes:
            ipc_block = " OR ".join([f'"{code}"*' for code in self.ipc_classes])
            if formatted_query:
                formatted_query += f" AND IC=({ipc_block})"
            else:
                formatted_query = f"IC=({ipc_block})"

        return formatted_query

    def generate_report_text(self, claim_chart_df: pd.DataFrame = None, prior_art_data: dict = None) -> str:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        lines = [
            "=" * 85,
            f"專利檢索與技術特徵分析報告 (含 Claims 檢核表、比對矩陣與前案附錄)",
            f"產出時間：{now}",
            "=" * 85,
            f"\n【一、發明標的與分類設定】",
            f"標的名稱：{self.target_title}",
            f"IPC 分類號：{', '.join(self.ipc_classes) if self.ipc_classes else '未指定'}",
            f"CPC 分類號：{', '.join(self.cpc_classes) if self.cpc_classes else '未指定'}",
            f"\n【二、技術特徵三支柱展開】",
        ]
        
        for idx, p in enumerate(self.pillars, 1):
            lines.append(f"  {idx}. {p.name}")
            lines.append(f"     - 英文關鍵字：{', '.join(p.en_keywords) if p.en_keywords else '無'}")
            lines.append(f"     - 中文關鍵字：{', '.join(p.zh_keywords) if p.zh_keywords else '無'}")

        lines.extend([
            f"\n【三、各平台布林檢索邏輯式】",
            f"▶ Google Patents / Espacenet 檢索語法：",
            f"{self.to_google_patents_query()}\n",
            f"▶ 台灣智慧財產局 (GPSS) 檢索語法：",
            f"{self.to_gpss_query()}",
            f"\n" + "=" * 85,
            f"【四、申請專利範圍（Claims）初稿撰寫合規檢核表】",
            f"=" * 85,
            f"[ ] 1. 標的定性清楚：獨立項前言（Preamble）是否清楚載明法定標的類型（物/裝置/系統/方法）？",
            f"[ ] 2. 過渡詞適切性：是否優先採用開放式過渡詞「包含（comprising）」，避免非必要之閉鎖限制？",
            f"[ ] 3. 獨立項最小特徵原則：獨立項（Claim 1）是否只記載達成發明目的之必要技術特徵，未塞入非必要優化參數？",
            f"[ ] 4. 名詞前置依據（Antecedent Basis）：所有冠上「該（the/said）」之元件，先前是否皆有一致之首次引入（一...）？",
            f"[ ] 5. 附屬項層級防禦：附屬項是否由寬至窄收斂，為進步性答辯與被核駁時預留明確退路（Fallback Positions）？",
            f"[ ] 6. 功效用語避免：專利範圍主體中是否避免出現「達到省電優勢」、「可增加30%效率」等純功效/宣傳性字眼？",
            f"\n" + "=" * 85,
            f"【五、全要件原則（All-Elements Rule）前案比對分析矩陣】",
            f"=" * 85,
        ])

        if claim_chart_df is not None and not claim_chart_df.empty:
            lines.append(claim_chart_df.to_string(index=False))
        else:
            lines.append("（尚未建立比對要件資料）")

        lines.extend([
            f"\n\n【比對結論備註】：",
            f"1. 字面侵權/缺乏新穎性判定：前案是否完全讀取了本發明 Claim 1 的所有 Element？",
            f"2. 進步性差異點（Distinguishing Features）：標註出哪一個 Element 具備非顯而易知性之技術突破。",
            "=" * 85
        ])

        # 附錄：爬取之引證前案原文
        lines.extend([
            f"\n【六、引證前案原文摘錄（附錄 Appendix）】",
            "=" * 85,
        ])

        if prior_art_data:
            lines.extend([
                f"專利號碼：{prior_art_data.get('patent_no', '未知')}",
                f"專利名稱：{prior_art_data.get('title', '未知')}",
                f"線上來源：{prior_art_data.get('url', '未知')}",
                f"\n--- 說明書摘要 (Abstract) ---",
                f"{prior_art_data.get('abstract', '無摘要內容')}",
                f"\n--- 申請專利範圍原文 (Claims) ---",
                f"{prior_art_data.get('claims', '無 Claims 內容')}",
                "=" * 85
            ])
        else:
            lines.extend([
                "（本次分析未執行線上前案專利爬取，或尚未載入引證專利原文資料）",
                "=" * 85
            ])

        return "\n".join(lines)

# ==============================================================================
# 二、 專利號爬取與內容解析
# ==============================================================================
def fetch_patent_data_from_google(patent_no: str) -> dict:
    """從 Google Patents 爬取專利名稱、摘要與申請專利範圍"""
    clean_pno = re.sub(r'[\s\-_/]', '', patent_no).upper()
    url = f"https://patents.google.com/patent/{clean_pno}/en"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    resp = requests.get(url, headers=headers, timeout=12)
    if resp.status_code != 200:
        raise Exception(f"無法取得專利資料 (HTTP {resp.status_code})，請確認專利號碼是否正確。")

    soup = BeautifulSoup(resp.text, "html.parser")

    title_elem = soup.find("meta", {"name": "DC.title"})
    title = title_elem["content"].strip() if title_elem and "content" in title_elem.attrs else ""
    if not title:
        h1 = soup.find("h1")
        title = h1.get_text(strip=True) if h1 else clean_pno

    abstract_sec = soup.find("section", {"itemprop": "abstract"})
    abstract = abstract_sec.get_text(separator="\n", strip=True) if abstract_sec else "（未擷取到摘要內容）"

    claims_sec = soup.find("section", {"itemprop": "claims"})
    claims_text = claims_sec.get_text(separator="\n", strip=True) if claims_sec else ""
    if not claims_text:
        c_elems = soup.find_all("div", class_="claim-text")
        claims_text = "\n".join([c.get_text(strip=True) for c in c_elems[:10]])

    return {
        "patent_no": clean_pno,
        "title": title,
        "abstract": abstract[:3000],
        "claims": claims_text[:6000],
        "url": url
    }

# ==============================================================================
# 三、 Gemini AI 自動重試與備援輪替封裝 (鎖定 3.6-flash 端點)
# ==============================================================================
CANDIDATE_MODELS = [
    "gemini-3.6-flash",
    "gemini-3.8-flash",
    "gemini-3.5-flash"
]

def generate_with_fallback(client, prompt: str) -> str:
    """依序嘗試 CANDIDATE_MODELS 清單中的模型，遇 503/429 退避重試"""
    last_exception = None
    for model_name in CANDIDATE_MODELS:
        for attempt in range(2):
            try:
                response = client.models.generate_content(
                    model=model_name,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json"
                    )
                )
                if response and response.text:
                    return response.text
            except Exception as e:
                last_exception = e
                err_str = str(e)
                if any(code in err_str for code in ["503", "429", "UNAVAILABLE", "RESOURCE_EXHAUSTED"]):
                    time.sleep(2)
                    continue
                if "404" in err_str or "NOT_FOUND" in err_str:
                    break
                break
    raise last_exception if last_exception else Exception("所有備援模型皆無法呼叫，請檢查 API Key 權限。")

def analyze_patent_with_gemini(api_key: str, title: str) -> dict:
    """專利特徵與 IPC/CPC 拆解"""
    client = genai.Client(api_key=api_key)
    prompt = f"""
    你是一名專業的專利代理人與資深專利檢索專家。
    請分析以下發明專利標的名稱，並以繁體中文與專業英文進行技術三支柱拆解、分類號建議與 Claim 1 要件拆解。

    發明標的名稱："{title}"

    請嚴格依照以下 JSON 結構回傳：
    {{
        "ipc": "建議的 IPC 分類號，用逗號隔開 (如 A01G 9/24, G01N 21/84)",
        "cpc": "建議的 CPC 分類號，用逗號隔開",
        "pillar_a_name": "Target: 標的名稱",
        "pillar_a_en": "英文關鍵字5~7個，逗號隔開",
        "pillar_a_zh": "中文同義詞5~7個，逗號隔開",
        "pillar_b_name": "Mechanism: 核心手段名稱",
        "pillar_b_en": "英文關鍵字5~7個，逗號隔開",
        "pillar_b_zh": "中文同義詞5~7個，逗號隔開",
        "pillar_c_name": "Effect: 技術功效名稱",
        "pillar_c_en": "英文關鍵字5~7個，逗號隔開",
        "pillar_c_zh": "中文同義詞5~7個，逗號隔開",
        "claim_elements": [
            {{
                "要件編號": "Element 1A",
                "本案 Claim 1 技術要件": "具體構件描繪",
                "前案 D1 對應技術": "",
                "前案 D2 對應技術": "",
                "符合性判定": "待確認",
                "差異/進步性說明": "預估發明點或技術功效說明"
            }}
        ]
    }}
    """
    res_text = generate_with_fallback(client, prompt)
    return json.loads(res_text)

def map_prior_art_with_gemini(api_key: str, current_elements: list, prior_art_data: dict, target_col: str) -> list:
    """使用 Gemini 將爬取之前案內容與目前 Claim 1 各要件進行比對"""
    client = genai.Client(api_key=api_key)
    prompt = f"""
    你是一名資深專利代理人，正在執行「全要件原則（All-Elements Rule）」專利侵權與新穎性/進步性比對。
    
    【本案 Claim 1 現有要件清單】：
    {json.dumps(current_elements, ensure_ascii=False, indent=2)}

    【爬取到的引證前案資訊】：
    專利號：{prior_art_data['patent_no']}
    發明名稱：{prior_art_data['title']}
    摘要：{prior_art_data['abstract']}
    專利範圍：{prior_art_data['claims'][:3000]}

    請仔細研讀前案內容，針對本案上述每一個要件（Element），提取該前案中是否有相對應之技術構件。
    請嚴格回傳一個 JSON 陣列，長度必須與本案要件清單完全相同，格式如下：
    [
        {{
            "matched_tech": "前案在此要件揭露的具體對應構件或手段（若未揭露請寫『未揭露』）",
            "judgment": "YES (字面讀取) / NO (不符/差異點) / 均等成立 (DOE)",
            "diff_note": "針對該要件之差異分析或進步性技術功效"
        }}
    ]
    """
    res_text = generate_with_fallback(client, prompt)
    return json.loads(res_text)

def analyze_trademark_with_gemini(api_key: str, brand_name: str, product_desc: str) -> dict:
    """商標識別性評估與尼斯分類對應"""
    client = genai.Client(api_key=api_key)
    prompt = f"""
    你是一名專業的商標代理人與智財法務專家。
    請分析以下商標名稱與其應用之產品/服務，評估其於台灣智慧財產局 (TIPO) 之申請可行性：

    商標名稱："{brand_name}"
    產品/技術描述："{product_desc}"

    請嚴格依照以下 JSON 結構回傳：
    {{
        "distinctiveness_level": "獨創性(Fanciful) / 任意性(Arbitrary) / 暗示性(Suggestive) / 說明性(Descriptive)",
        "legal_risk_analysis": "針對該名稱之核駁風險與審查注意事項簡析 (100字內)",
        "nice_classes": [
            {{
                "class_num": "第 09 類",
                "group_codes": "0901, 0904",
                "recommended_items": "光學感測儀器、農業用病害監測軟體、邊緣運算處理器"
            }},
            {{
                "class_num": "第 42 類",
                "group_codes": "4209",
                "recommended_items": "軟體即服務(SaaS)、農業光學數據分析、雲端推論平台"
            }}
        ],
        "clearance_search_keywords": "建議於 TIPO 檢索時比對的文字或同音異字 (逗號隔開)"
    }}
    """
    res_text = generate_with_fallback(client, prompt)
    return json.loads(res_text)

# ==============================================================================
# 四、 商標圖樣繪製核心邏輯 (支援上傳 Logo 圖文合成，符合 TIPO 規範)
# ==============================================================================
def create_tipo_trademark_bytes(
    text: str,
    layout: str = "純文字：單行水平置中",
    font_size: int = 76,
    logo_file=None
) -> bytes:
    """
    產生符合 TIPO 電子送件 8x8 cm 300DPI (945x945 px) 規格之 JPEG bytes。
    支援純文字、文字上下分行、以及上圖下文/左圖右文等複合 Logo 排版。
    """
    dpi = 300
    cm_to_inch = 2.54
    width_px = int((8.0 / cm_to_inch) * dpi)
    height_px = int((8.0 / cm_to_inch) * dpi)

    canvas = Image.new("RGB", (width_px, height_px), color=(255, 255, 255))
    draw = ImageDraw.Draw(canvas)

    candidate_fonts = [
        "C:/Windows/Fonts/msjh.ttc",
        "C:/Windows/Fonts/msjhbd.ttc",
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/STHeiti Light.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    ]
    font = None
    for f in candidate_fonts:
        if os.path.exists(f):
            try:
                font = ImageFont.truetype(f, font_size)
                break
            except Exception:
                continue
    if font is None:
        font = ImageFont.load_default()

    logo_img = None
    if logo_file is not None:
        try:
            uploaded_logo = Image.open(logo_file)
            if uploaded_logo.mode in ("RGBA", "LA") or (uploaded_logo.mode == "P" and "transparency" in uploaded_logo.info):
                rgba_logo = uploaded_logo.convert("RGBA")
                white_bg = Image.new("RGBA", rgba_logo.size, (255, 255, 255, 255))
                logo_img = Image.alpha_composite(white_bg, rgba_logo).convert("RGB")
            else:
                logo_img = uploaded_logo.convert("RGB")
        except Exception:
            logo_img = None

    if logo_img and layout == "複合商標：上圖下文":
        target_logo_h = int(height_px * 0.45)
        aspect = logo_img.width / logo_img.height
        new_w = int(target_logo_h * aspect)
        if new_w > int(width_px * 0.75):
            new_w = int(width_px * 0.75)
            target_logo_h = int(new_w / aspect)
        resized_logo = logo_img.resize((new_w, target_logo_h), Image.Resampling.LANCZOS)

        bbox = draw.textbbox((0, 0), text, font=font)
        text_w = bbox[2] - bbox[0]
        text_h = bbox[3] - bbox[1]

        spacing = int(height_px * 0.05)
        total_block_h = target_logo_h + spacing + text_h
        start_y = (height_px - total_block_h) // 2

        logo_x = (width_px - new_w) // 2
        canvas.paste(resized_logo, (logo_x, start_y))

        text_x = (width_px - text_w) // 2 - bbox[0]
        text_y = start_y + target_logo_h + spacing - bbox[1]
        draw.text((text_x, text_y), text, font=font, fill=(0, 0, 0))

    elif logo_img and layout == "複合商標：左圖右文":
        target_logo_w = int(width_px * 0.35)
        aspect = logo_img.height / logo_img.width
        new_h = int(target_logo_w * aspect)
        if new_h > int(height_px * 0.6):
            new_h = int(height_px * 0.6)
            target_logo_w = int(new_h / aspect)
        resized_logo = logo_img.resize((target_logo_w, new_h), Image.Resampling.LANCZOS)

        bbox = draw.textbbox((0, 0), text, font=font)
        text_w = bbox[2] - bbox[0]
        text_h = bbox[3] - bbox[1]

        spacing = int(width_px * 0.04)
        total_block_w = target_logo_w + spacing + text_w
        start_x = (width_px - total_block_w) // 2

        logo_y = (height_px - new_h) // 2
        canvas.paste(resized_logo, (start_x, logo_y))

        text_x = start_x + target_logo_w + spacing - bbox[0]
        text_y = (height_px - text_h) // 2 - bbox[1]
        draw.text((text_x, text_y), text, font=font, fill=(0, 0, 0))

    elif layout == "純文字：上下雙行置中":
        parts = text.strip().split(maxsplit=1)
        line1 = parts[0] if len(parts) > 0 else ""
        line2 = parts[1] if len(parts) > 1 else ""

        bbox1 = draw.textbbox((0, 0), line1, font=font)
        w1, h1 = bbox1[2] - bbox1[0], bbox1[3] - bbox1[1]

        bbox2 = draw.textbbox((0, 0), line2, font=font)
        w2, h2 = bbox2[2] - bbox2[0], bbox2[3] - bbox2[1]

        line_spacing = int(font_size * 0.4)
        total_h = h1 + h2 + line_spacing
        start_y = (height_px - total_h) / 2
        draw.text(((width_px - w1) / 2 - bbox1[0], start_y - bbox1[1]), line1, font=font, fill=(0, 0, 0))
        draw.text(((width_px - w2) / 2 - bbox2[0], start_y + h1 + line_spacing - bbox2[1]), line2, font=font, fill=(0, 0, 0))
    else:
        bbox = draw.textbbox((0, 0), text, font=font)
        w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
        x = (width_px - w) / 2 - bbox[0]
        y = (height_px - h) / 2 - bbox[1]
        draw.text((x, y), text, font=font, fill=(0, 0, 0))

    img_buffer = io.BytesIO()
    canvas.save(img_buffer, format="JPEG", dpi=(dpi, dpi), quality=95, subsampling=0)
    return img_buffer.getvalue()

# ==============================================================================
# 五、 複製輔助元件 (Clipboard)
# ==============================================================================
def render_copy_button(text_to_copy: str, button_label: str = "📋 點擊複製", button_id: str = "copyBtn"):
    escaped_text = text_to_copy.replace("\\", "\\\\").replace("`", "\\`").replace("$", "\\$")
    html_code = f"""
    <div style="margin-bottom: 10px;">
        <button id="{button_id}" style="
            width: 100%;
            background-color: #f0f2f6;
            color: #31333F;
            border: 1px solid #d6d6d8;
            border-radius: 8px;
            padding: 8px 16px;
            font-size: 14px;
            font-weight: 500;
            cursor: pointer;
            transition: all 0.2s ease;
        ">{button_label}</button>
    </div>
    <script>
        const btn_{button_id} = document.getElementById("{button_id}");
        btn_{button_id}.addEventListener("click", async () => {{
            try {{
                await navigator.clipboard.writeText(`{escaped_text}`);
                btn_{button_id}.innerText = "✅ 已複製至剪貼簿！";
                btn_{button_id}.style.backgroundColor = "#e6f4ea";
                btn_{button_id}.style.color = "#137333";
                setTimeout(() => {{
                    btn_{button_id}.innerText = "{button_label}";
                    btn_{button_id}.style.backgroundColor = "#f0f2f6";
                    btn_{button_id}.style.color = "#31333F";
                }}, 2000);
            }} catch (err) {{
                console.error("複製失敗:", err);
            }}
        }});
    </script>
    """
    components.html(html_code, height=50)

# ==============================================================================
# 六、 智財核心法規資料庫 (專利法 ＆ 商標法常用條文)
# ==============================================================================
IP_LAWS_DB = [
    {
        "category": "專利法",
        "article": "專利法 第 21 條",
        "title": "發明之定義",
        "keywords": "自然法則, 技術思想, 發明",
        "text": "本法所稱發明，指利用自然法則之技術思想之創作。",
        "explanation": "發明必須是「利用自然法則」之技術創作。純粹之數學公式、商業模式、人為遊戲規則、純電腦演算法或非利用自然法則者，無法單獨取得發明專利。"
    },
    {
        "category": "專利法",
        "article": "專利法 第 22 條",
        "title": "專利三要件（產業利用性、新穎性、進步性）",
        "keywords": "新穎性, 進步性, 產業利用性, 公開, 容易完成",
        "text": (
            "可供產業上利用之發明，無下列情事之一，得依本法申請專利：\n"
            "一、申請前已見於刊物者。\n"
            "二、申請前已公開實施者。\n"
            "三、申請前已為公眾所知悉者。\n\n"
            "發明雖無前項各款所列情事，但為其所屬技術領域中具有通常知識者依申請前之先前技術所能輕易完成時，仍不得依本法申請專利。"
        ),
        "explanation": "【實務要點】第1項規範「新穎性」（單一前案不可完全揭露所有技術特徵）；第2項規範「進步性」（所屬技術領域具通常知識者無法依多份前案結合輕易完成）。"
    },
    {
        "category": "專利法",
        "article": "專利法 第 26 條",
        "title": "說明書之充分揭露與申請專利範圍之明確性",
        "keywords": "說明書, 申請專利範圍, 明確, 充分揭露, 支持",
        "text": (
            "說明書應明確且充分揭露，使該發明所屬技術領域中具有通常知識者，能瞭解其內容，並可據以實現。\n"
            "申請專利範圍應界定申請專利之發明；其得包括一項以上之請求項，各請求項應以明確、簡潔之方式記載，且必須為說明書所支持。"
        ),
        "explanation": "獨立項不可記載不明確或宣傳性功效用語；說明書必須達到「可據以實現（Enablement）」門檻，否則將依本條核駁或提起無效舉發。"
    },
    {
        "category": "專利法",
        "article": "專利法 第 58 條",
        "title": "專利權人之排他專有權限",
        "keywords": "專利權, 排他權, 製造, 為販賣之要約, 販賣, 使用, 輸入",
        "text": (
            "專利權人，除本法另有規定外，專有排除他人未經其同意而製造、為販賣之要約、販賣、使用或為上述目的而進口該發明之權。\n"
            "物之發明，其專利權範圍不及於以該物為標的所生產之產品。"
        ),
        "explanation": "專利權本質上為「排除他人未經同意實施」之消極排他權，而非保證自己實施時絕不侵害他人專利。"
    },
    {
        "category": "專利法",
        "article": "專利法 第 59 條",
        "title": "專利權效力之限制（合理使用）",
        "keywords": "效力限制, 非營利, 研究, 試驗, 藥品查驗",
        "text": (
            "發明專利權之效力，不及於下列各款情事：\n"
            "一、非出於商業目的之未公開行為。\n"
            "二、以研究或實驗為目的實施發明之必要行為。\n"
            "三、在專利申請日前，在國內已實施該發明，或已完成必須之準備者（先使用權）。"
        ),
        "explanation": "非商業目的之學術研發或學術試驗行為不受專利權拘束；若在他人專利申請日前已在國內量產或完成準備，得主張先使用權。"
    },
    {
        "category": "商標法",
        "article": "商標法 第 18 條",
        "title": "商標之定義與識別性基本原則",
        "keywords": "商標, 識別性, 表彰, 商品, 服務",
        "text": (
            "商標，指任何具有識別性之標識，得以文字、圖形、記號、顏色、立體形狀、動態、全像圖、聲音等，或其聯合式所組成。\n"
            "前項所稱識別性，指足以使商品或服務之相關消費者認識為指示商品或服務來源，並得與他人之商品或服務相區別者。"
        ),
        "explanation": "商標的核心靈魂為「識別性（Distinctiveness）」，必須能讓消費者將其視為品牌標識，而非單純的商品名稱或廣告宣傳口號。"
    },
    {
        "category": "商標法",
        "article": "商標法 第 29 條",
        "title": "不得註冊商標之事由（缺乏先天識別性）",
        "keywords": "說明性, 描述性, 通用名稱, 先天識別性, 後天識別性",
        "text": (
            "商標有下列情形之一，不得註冊：\n"
            "一、僅由說明所指定商品或服務之品質、用途、原料、產地或相關特性之標識所組成者。\n"
            "二、僅由所指定商品或服務之通用名稱或形狀所組成者。\n"
            "三、僅由其他不具識別性之標識所組成者。\n\n"
            "有前項各款規定之情形，如經申請人使用且在交易上已成為商品或服務之識別標識者，不在此限（後天識別性）。"
        ),
        "explanation": "直接描述產品功能（如在水果賣場註冊「鮮甜可口」）欠缺先天識別性；但若經長期大規模商業行銷使公眾認知其為品牌（例如「黑貓宅急便」），可依第2項主張後天識別性取得註冊。"
    },
    {
        "category": "商標法",
        "article": "商標法 第 30 條 第 1 項 第 10 款",
        "title": "相對不得註冊事由（致相關消費者混淆誤認之虞）",
        "keywords": "混淆誤認, 相同, 近似, 先申請, 同一, 類似",
        "text": (
            "商標有下列情形之一，不得註冊：\n"
            "十、相同或近似於他人同一或類似商品或服務之註冊商標或申請在先之商標，有致相關消費者混淆誤認之虞者。"
        ),
        "explanation": "這是商標核駁與異議最常見的條款。審查時會考量：商標圖樣外觀/讀音/觀念近似程度、商品或服務類似程度、先權利商標之著名程度等多重因素。"
    },
    {
        "category": "商標法",
        "article": "商標法 第 30 條 第 1 項 第 11 款",
        "title": "著名商標之保護（淡化與仿冒防範）",
        "keywords": "著名商標, 減損識別性, 減損信譽, 淡化, 仿冒",
        "text": (
            "商標有下列情形之一，不得註冊：\n"
            "十一、相同或近似於他人著名商標或標章，有致相關公眾混淆誤認之虞，或有減損著名商標或標章之識別性或信譽之虞者。"
        ),
        "explanation": "著名商標享跨類別擴張保護。即使商品或服務類別不相同，若使用他人著名品牌容易造成稀釋（Dilution）或減損商譽者，同樣不得註冊。"
    },
    {
        "category": "商標法",
        "article": "商標法 第 68 條",
        "title": "侵害商標權之行為態樣",
        "keywords": "侵權, 排除侵害, 混淆誤認, 使用商標",
        "text": (
            "未得商標權人同意，有下列情形之一，為侵害商標權：\n"
            "一、於同一商品或服務，使用相同於註冊商標之商標者。\n"
            "二、於類似之商品或服務，使用相同於註冊商標之商標，有致相關消費者混淆誤認之虞者。\n"
            "三、於同一或類似之商品或服務，使用近似於註冊商標之商標，有致相關消費者混淆誤認之虞者。"
        ),
        "explanation": "於同類商品使用相同商標屬典型直接侵權；於類似商品或使用近似商標，則以「致相關消費者混淆誤認之虞」為實質侵權成立之核心判定。"
    }
]

# ==============================================================================
# 七、 Streamlit 介面配置
# ==============================================================================
st.set_page_config(
    page_title="智慧財產權整合工作台 (專利 ＆ 商標)",
    page_icon="🛡️",
    layout="wide"
)

st.title("🛡️ 智慧財產權整合工作台 (專利 ＆ 商標)")
st.markdown("結合 **Google Patents 邏輯檢索**、**專利號自動爬取對應**、**Claims 全要件比對矩陣**、**TIPO 規範圖樣生成** 與 **智財法規速查**。")

# 側邊欄金鑰設定
st.sidebar.header("🔑 Gemini API 設定")
secret_key = ""
if "GEMINI_API_KEY" in st.secrets:
    secret_key = st.secrets["GEMINI_API_KEY"]
elif "GEMINI_API_KEY" in os.environ:
    secret_key = os.environ["GEMINI_API_KEY"]

user_api_key = st.sidebar.text_input(
    "請輸入 Google Gemini API Key：",
    value=secret_key,
    type="password",
    help="可在 Google AI Studio (aistudio.google.com) 免費申請 API Key。"
)

tab_patent, tab_trademark, tab_laws = st.tabs([
    "📄 專利檢索與 Claims 比對矩陣",
    "🏷️ 商標權佈局與圖樣生成器",
    "⚖️ 智財法規速查 (專利法 ＆ 商標法)"
])

# ==============================================================================
# TAB 1: 專利權模組
# ==============================================================================
with tab_patent:
    st.sidebar.markdown("---")
    st.sidebar.header("📁 專利技術範本")
    template = st.sidebar.selectbox(
        "選擇技術模板快速填入：",
        [
            "自訂輸入",
            "多光譜溫室作物病害早期偵測系統",
            "邊緣運算光學瑕疵檢測",
            "無鏈條齒輪箱無段變速花轂"
        ]
    )

    if "form_data" not in st.session_state:
        st.session_state.form_data = {
            "title": "", "ipc": "", "cpc": "",
            "p1_name": "Target: 應用標的", "p1_en": "", "p1_zh": "",
            "p2_name": "Mechanism: 核心手段/機構", "p2_en": "", "p2_zh": "",
            "p3_name": "Effect: 技術功效/特徵", "p3_en": "", "p3_zh": "",
            "claims": []
        }

    if "last_fetched_patent" not in st.session_state:
        st.session_state.last_fetched_patent = None

    if template == "多光譜溫室作物病害早期偵測系統":
        st.session_state.form_data.update({
            "title": "多光譜溫室作物病害早期偵測系統",
            "ipc": "A01G 9/24, G01N 21/84, G06V 20/10, G06T 7/00",
            "cpc": "A01G 9/24, G01N 2021/8466, G06V 20/188",
            "p1_name": "Target: 溫室作物與植物病害",
            "p1_en": "greenhouse crop, plant disease, foliage pathogen, tomato crop, crop health",
            "p1_zh": "溫室作物, 植物病害, 葉片病原, 作物健康, 番茄病害",
            "p2_name": "Mechanism: 多光譜感測與邊緣影像推論",
            "p2_en": "multispectral imaging, hyperspectral sensor, narrowband reflectance, edge computing, deep learning inference",
            "p2_zh": "多光譜影像, 高光譜感測, 窄波段反射率, 邊緣運算, 深度學習推論",
            "p3_name": "Effect: 潛伏早期偵測與即時警報",
            "p3_en": "early lesion detection, asymptomatic stage, pre-symptomatic diagnosis, real-time alert, false alarm reduction",
            "p3_zh": "早期病斑偵測, 潛伏期診斷, 症狀前檢測, 即時告警, 降低誤判",
            "claims": [
                {"要件編號": "Element 1A", "本案 Claim 1 技術要件": "一多光譜感測模組，配置於移動軌道，具有特定吸收峰窄波段濾波感測器", "前案 D1 對應技術": "常規 RGB 廣角監視器", "前案 D2 對應技術": "手持式分光輻射計", "符合性判定": "NO (不符/差異點)", "差異/進步性說明": "本案特定窄波段針對植物水分及葉綠素吸收峰，非可見光全光譜影像"},
                {"要件編號": "Element 1B", "本案 Claim 1 技術要件": "一邊緣推論處理器，對多光譜影像執行植被指數（NDVI/PRI）正規化降維校正", "前案 D1 對應技術": "影像壓縮後直接回傳伺服器", "前案 D2 對應技術": "離線電腦以 MATLAB 批次運算", "符合性判定": "NO (不符/差異點)", "差異/進步性說明": "本案於感測端完成即時反光補償與植被指數特徵化，降低傳輸頻寬"},
                {"要件編號": "Element 1C", "本案 Claim 1 技術要件": "一病斑早期預警神經網路模型，根據特徵化多光譜資訊預測前症狀潛伏病灶", "前案 D1 對應技術": "色差比對判定枯黃斑塊", "前案 D2 對應技術": "葉片病徵分類 CNN", "符合性判定": "NO (不符/差異點)", "差異/進步性說明": "本案能在葉片肉眼尚未顯性變色前 48 小時識別隱性病原感染"},
                {"要件編號": "Element 1D", "本案 Claim 1 技術要件": "一環控連動介面，當接收預警訊號時觸發特定分區通風調節與精準噴灑", "前案 D1 對應技術": "警報訊息推播至使用者手機", "前案 D2 對應技術": "全區定時自動噴灌", "符合性判定": "YES (字面讀取)", "差異/進步性說明": "結合定位分區執行隔離防護之閉迴路控制"}
            ]
        })
    elif template == "邊緣運算光學瑕疵檢測":
        st.session_state.form_data.update({
            "title": "基於邊緣運算之即時影像瑕疵檢測系統",
            "ipc": "G06T 7/00, G01N 21/88", "cpc": "G06V 10/00",
            "p1_name": "Target: 瑕疵檢測", "p1_en": "defect detection, flaw inspection, surface anomaly", "p1_zh": "瑕疵檢測, 缺陷檢驗, 表面異常",
            "p2_name": "Mechanism: 邊緣運算與視覺推論", "p2_en": "edge computing, neural network, real-time inferenc*", "p2_zh": "邊緣運算, 神經網絡, 即時推論, 深度學習",
            "p3_name": "Effect: 低延遲與高精度", "p3_en": "low latency, high throughput, false positive reduction", "p3_zh": "低延遲, 降低誤判, 即時處理",
            "claims": [
                {"要件編號": "Element 1A", "本案 Claim 1 技術要件": "一工業高速相機，擷取產線物件表面光學影像", "前案 D1 對應技術": "CCD 線型感測器", "前案 D2 對應技術": "面陣相機", "符合性判定": "YES (字面讀取)", "差異/進步性說明": "公知取像構件"},
                {"要件編號": "Element 1B", "本案 Claim 1 技術要件": "一邊緣推論加速模組，具備特定神經網路剪枝架構", "前案 D1 對應技術": "工控機 GPU 集中運算", "前案 D2 對應技術": "雲端伺服器推論", "符合性判定": "NO (不符/差異點)", "差異/進步性說明": "邊緣端低功耗輕量化推論"},
                {"要件編號": "Element 1C", "本案 Claim 1 技術要件": "一動態閾值缺陷分割演算法，抑制表面反光雜訊", "前案 D1 對應技術": "固定灰階值二值化", "前案 D2 對應技術": "局部自適應閥值", "符合性判定": "均等成立 (DOE)", "差異/進步性說明": "進一步考量動態曝光補償"}
            ]
        })
    elif template == "無鏈條齒輪箱無段變速花轂":
        st.session_state.form_data.update({
            "title": "無鏈條傳動之齒輪箱無段變速自行車花轂",
            "ipc": "B62M 17/00, B62M 11/16, F16H 15/52", "cpc": "",
            "p1_name": "Target: 自行車與花轂", "p1_en": "bicycle, bike, bicycle hub, wheel hub", "p1_zh": "自行車, 腳踏車, 車轂, 花轂, 輪轂",
            "p2_name": "Mechanism: 無鏈條傳動與齒輪箱", "p2_en": "chainless, shaft drive, transmission shaft, gearbox, bevel gear", "p2_zh": "無鏈, 軸傳動, 傳動軸, 齒輪箱, 傘齒輪",
            "p3_name": "Mechanism: 無段變速 (CVT)", "p3_en": "continuously variable, CVT, infinitely variable, friction drive, traction drive", "p3_zh": "無段變速, 無級變速, 摩擦傳動, 球體傳動",
            "claims": [
                {"要件編號": "Element 1A", "本案 Claim 1 技術要件": "一固定軸，定義有一中心旋轉軸線", "前案 D1 對應技術": "揭露後輪固定軸心", "前案 D2 對應技術": "揭露中空固定主軸", "符合性判定": "YES (字面讀取)", "差異/進步性說明": "公知構件"},
                {"要件編號": "Element 1B", "本案 Claim 1 技術要件": "一外輪轂殼體，可相對固定軸旋轉套設", "前案 D1 對應技術": "鋁合金花轂外殼", "前案 D2 對應技術": "車輪外殼體", "符合性判定": "YES (字面讀取)", "差異/進步性說明": "公知構件"},
                {"要件編號": "Element 1C", "本案 Claim 1 技術要件": "一動力輸入齒輪組，具有封閉箱體內之輸入齒輪", "前案 D1 對應技術": "外露飛輪，無封閉箱體", "前案 D2 對應技術": "傘齒輪組，但未封閉", "符合性判定": "NO (不符/差異點)", "差異/進步性說明": "本案封閉箱體具防塵與扭矩支撐之特殊功效"},
                {"要件編號": "Element 1D", "本案 Claim 1 技術要件": "一無段變速機構，具複數轉動球體進行傳動調節", "前案 D1 對應技術": "傳統階梯齒輪變速", "前案 D2 對應技術": "球體 CVT 機構", "符合性判定": "待確認", "差異/進步性說明": "與封閉齒輪箱之同軸緊湊整合為主要發明點"}
            ]
        })

    st.subheader("1. 發明標的名稱與 AI 自動拆解")
    col_input1, col_input2 = st.columns([3, 1])

    with col_input1:
        target_title = st.text_input(
            "請輸入專利標的名稱：",
            value=st.session_state.form_data["title"],
            placeholder="例如：晶圓搬運機械手臂動態抑振控制系統"
        )

    with col_input2:
        st.write("")
        st.write("")
        ai_btn = st.button("✨ Gemini AI 自動拆解", type="secondary", use_container_width=True, key="btn_patent_ai")

    if ai_btn:
        if not user_api_key.strip():
            st.error("請先於左側側邊欄輸入有效的 Gemini API Key！")
        elif not target_title.strip():
            st.warning("請先輸入專利標的名稱。")
        else:
            with st.spinner("🤖 正在調用 Gemini 拆解技術特徵..."):
                try:
                    ai_res = analyze_patent_with_gemini(user_api_key.strip(), target_title.strip())
                    st.session_state.form_data.update({
                        "title": target_title.strip(),
                        "ipc": ai_res.get("ipc", ""),
                        "cpc": ai_res.get("cpc", ""),
                        "p1_name": ai_res.get("pillar_a_name", "Target: 應用標的"),
                        "p1_en": ai_res.get("pillar_a_en", ""),
                        "p1_zh": ai_res.get("pillar_a_zh", ""),
                        "p2_name": ai_res.get("pillar_b_name", "Mechanism: 核心手段"),
                        "p2_en": ai_res.get("pillar_b_en", ""),
                        "p2_zh": ai_res.get("pillar_b_zh", ""),
                        "p3_name": ai_res.get("pillar_c_name", "Effect: 技術功效"),
                        "p3_en": ai_res.get("pillar_c_en", ""),
                        "p3_zh": ai_res.get("pillar_c_zh", ""),
                        "claims": ai_res.get("claim_elements", [])
                    })
                    st.success("🎉 Gemini AI 拆解完成！相關欄位與 Claim Chart 已自動更新。")
                    st.rerun()
                except Exception as e:
                    st.error(f"AI 呼叫失敗，請稍後重試。詳細原因: {e}")

    col_class1, col_class2 = st.columns(2)
    with col_class1:
        ipc_input = st.text_input("IPC 分類號 (逗號隔開)", value=st.session_state.form_data["ipc"], placeholder="例: A01G 9/24, G01N 21/84")
    with col_class2:
        cpc_input = st.text_input("CPC 分類號 (逗號隔開)", value=st.session_state.form_data["cpc"], placeholder="例: G06V 20/188")

    st.markdown("---")
    st.subheader("2. 技術三支柱特徵拆解")

    col_p1, col_p2, col_p3 = st.columns(3)
    with col_p1:
        st.markdown("#### 支柱 A：應用標的 (Target)")
        p1_name = st.text_input("支柱 A 名稱", value=st.session_state.form_data["p1_name"], key="p1_n")
        p1_en = st.text_area("英文關鍵字 (逗號隔開)", value=st.session_state.form_data["p1_en"], key="p1_e", height=100)
        p1_zh = st.text_area("中文關鍵字 (逗號隔開)", value=st.session_state.form_data["p1_zh"], key="p1_z", height=100)

    with col_p2:
        st.markdown("#### 支柱 B：核心手段 (Mechanism)")
        p2_name = st.text_input("支柱 B 名稱", value=st.session_state.form_data["p2_name"], key="p2_n")
        p2_en = st.text_area("英文關鍵字 (逗號隔開)", value=st.session_state.form_data["p2_en"], key="p2_e", height=100)
        p2_zh = st.text_area("中文關鍵字 (逗號隔開)", value=st.session_state.form_data["p2_zh"], key="p2_z", height=100)

    with col_p3:
        st.markdown("#### 支柱 C：技術功效 (Effect)")
        p3_name = st.text_input("支柱 C 名稱", value=st.session_state.form_data["p3_name"], key="p3_n")
        p3_en = st.text_area("英文關鍵字 (逗號隔開)", value=st.session_state.form_data["p3_en"], key="p3_e", height=100)
        p3_zh = st.text_area("中文關鍵字 (逗號隔開)", value=st.session_state.form_data["p3_zh"], key="p3_z", height=100)

    st.markdown("---")

    # ==========================================================================
    # 3. 引證前案自動爬取與比對
    # ==========================================================================
    st.subheader("3. 引證前案專利號爬取與自動比對 (Auto-fetch Prior Art)")
    st.caption("輸入引證案公開/公告號（支援 US、EP、WO、TW 等），自動自 Google Patents 爬取內容，並由 AI 比對填入下表指定的前案欄位。")

    col_fetch1, col_fetch2, col_fetch3 = st.columns([2, 1, 1])
    with col_fetch1:
        target_pno = st.text_input("前案專利號 (公開號/公告號)：", placeholder="例如：US11234567B2、EP3567890A1、US20230012345A1", key="fetch_pno_input")
    with col_fetch2:
        target_slot = st.selectbox("填入比對欄位：", ["前案 D1 對應技術", "前案 D2 對應技術"], key="fetch_slot_select")
    with col_fetch3:
        st.write("")
        st.write("")
        fetch_btn = st.button("📥 爬取並自動填入", type="secondary", use_container_width=True)

    if fetch_btn:
        if not target_pno.strip():
            st.warning("請先輸入前案專利號。")
        elif not user_api_key.strip():
            st.error("自動技術特徵拆解需要 Gemini API Key，請先於左側側邊欄填入！")
        else:
            with st.spinner(f"🌐 正在爬取 {target_pno.strip()} 並啟動多模型備援比對..."):
                try:
                    p_data = fetch_patent_data_from_google(target_pno.strip())
                    st.session_state.last_fetched_patent = p_data

                    curr_claims = st.session_state.form_data.get("claims", [])
                    if not curr_claims:
                        curr_claims = [
                            {"要件編號": "Element 1A", "本案 Claim 1 技術要件": "主要機構/感測裝置", "前案 D1 對應技術": "", "前案 D2 對應技術": "", "符合性判定": "待確認", "差異/進步性說明": ""},
                            {"要件編號": "Element 1B", "本案 Claim 1 技術要件": "運算處理/特徵提取", "前案 D1 對應技術": "", "前案 D2 對應技術": "", "符合性判定": "待確認", "差異/進步性說明": ""},
                            {"要件編號": "Element 1C", "本案 Claim 1 技術要件": "輸出控制/閉迴路連動", "前案 D1 對應技術": "", "前案 D2 對應技術": "", "符合性判定": "待確認", "差異/進步性說明": ""}
                        ]

                    ai_mappings = map_prior_art_with_gemini(user_api_key.strip(), curr_claims, p_data, target_slot)

                    for idx, row in enumerate(curr_claims):
                        if idx < len(ai_mappings):
                            row[target_slot] = f"[{p_data['patent_no']}] " + ai_mappings[idx].get("matched_tech", "")
                            if ai_mappings[idx].get("judgment"):
                                row["符合性判定"] = ai_mappings[idx].get("judgment")
                            if ai_mappings[idx].get("diff_note"):
                                row["差異/進步性說明"] = ai_mappings[idx].get("diff_note")

                    st.session_state.form_data["claims"] = curr_claims
                    st.success(f"✅ 成功擷取專利：【{p_data['patent_no']}】{p_data['title']}，已完成對應比對！")
                    st.rerun()

                except Exception as e:
                    st.error(f"爬取或比對失敗: {e}")

    # 展開檢視爬取之專利原文
    if st.session_state.last_fetched_patent:
        last_p = st.session_state.last_fetched_patent
        with st.expander(f"📖 查看最近爬取之專利原文：【{last_p['patent_no']}】{last_p['title']}", expanded=True):
            col_info1, col_info2 = st.columns([3, 1])
            with col_info1:
                st.markdown(f"**專利名稱**：{last_p['title']}")
                st.markdown(f"**專利公開/公告號**：`{last_p['patent_no']}`")
            with col_info2:
                st.link_button("🌐 在 Google Patents 開啟原文", last_p["url"], use_container_width=True)

            st.markdown("##### 📄 專利說明書摘要 (Abstract)")
            st.info(last_p["abstract"] if last_p["abstract"] else "無摘要內容")

            st.markdown("##### ⚖️ 申請專利範圍原文 (Claims)")
            if last_p["claims"]:
                st.code(last_p["claims"], language="text")
            else:
                st.warning("未自該專利頁面擷取到 Claims 條文。")

    st.markdown("---")
    st.subheader("4. 申請專利範圍全要件比對矩陣 (線上編輯)")
    current_claims = st.session_state.form_data.get("claims", [])
    if not current_claims:
        current_claims = [
            {"要件編號": "Element 1A", "本案 Claim 1 技術要件": "", "前案 D1 對應技術": "", "前案 D2 對應技術": "", "符合性判定": "待確認", "差異/進步性說明": ""}
        ]

    edited_df = st.data_editor(
        pd.DataFrame(current_claims),
        num_rows="dynamic",
        use_container_width=True,
        column_config={
            "要件編號": st.column_config.TextColumn("要件編號", width="small", required=True),
            "本案 Claim 1 技術要件": st.column_config.TextColumn("本案 Claim 1 技術要件", width="medium"),
            "前案 D1 對應技術": st.column_config.TextColumn("前案 D1 對應技術", width="medium"),
            "前案 D2 對應技術": st.column_config.TextColumn("前案 D2 對應技術", width="medium"),
            "符合性判定": st.column_config.SelectboxColumn("符合性判定", options=["YES (字面讀取)", "NO (不符/差異點)", "均等成立 (DOE)", "待確認"], width="small"),
            "差異/進步性說明": st.column_config.TextColumn("差異分析 / 進步性技術功效", width="large"),
        },
        key=f"claim_editor_{template}_{len(current_claims)}"
    )

    st.markdown("---")
    if st.button("🚀 生成專利檢索式並整合比對報告", type="primary", use_container_width=True):
        builder = PatentSearchBuilder(target_title if target_title else "未命名技術標的")
        if ipc_input:
            builder.add_ipc(*ipc_input.split(","))
        if cpc_input:
            builder.add_cpc(*cpc_input.split(","))
        builder.add_pillar(p1_name, p1_en.split(",") if p1_en else [], p1_zh.split(",") if p1_zh else [])
        builder.add_pillar(p2_name, p2_en.split(",") if p2_en else [], p2_zh.split(",") if p2_zh else [])
        builder.add_pillar(p3_name, p3_en.split(",") if p3_en else [], p3_zh.split(",") if p3_zh else [])

        google_query = builder.to_google_patents_query()
        gpss_query = builder.to_gpss_query()
        
        report_text = builder.generate_report_text(
            claim_chart_df=edited_df,
            prior_art_data=st.session_state.get("last_fetched_patent")
        )

        st.subheader("📋 產出結果")
        col_res1, col_res2 = st.columns(2)
        with col_res1:
            st.markdown("#### 🌐 Google Patents / Espacenet 檢索式")
            st.code(google_query if google_query else "（無有效檢索式）", language="text")
            if google_query.strip():
                render_copy_button(google_query, "📋 快速複製 Google Patents 檢索式", button_id="copyGoogle")
                encoded_query = urllib.parse.quote_plus(google_query)
                st.link_button("🌐 一鍵前往 Google Patents 檢索", f"https://patents.google.com/?q={encoded_query}", type="secondary", use_container_width=True)

        with col_res2:
            st.markdown("#### 🇹🇼 台灣智慧局 GPSS 檢索式")
            st.code(gpss_query if gpss_query else "（無有效檢索式）", language="text")
            if gpss_query.strip():
                render_copy_button(gpss_query, "📋 快速複製 GPSS 檢索式", button_id="copyGPSS")
                st.link_button("🇹🇼 開啟台灣智慧局 GPSS 系統", "https://gpss.tipo.gov.tw/", type="secondary", use_container_width=True)

        st.markdown("---")
        st.subheader("💾 匯出專利報告檔")
        time_str = datetime.now().strftime('%Y%m%d_%H%M%S')
        csv_bytes = edited_df.to_csv(index=False, encoding="utf-8-sig").encode("utf-8-sig")

        col_dl1, col_dl2 = st.columns(2)
        with col_dl1:
            st.download_button(
                "📥 下載完整檢索分析報告 (.txt)",
                data=report_text,
                file_name=f"patent_analysis_{time_str}.txt",
                mime="text/plain",
                type="primary",
                use_container_width=True
            )
            st.caption("已包含：三支柱、檢索式、Claims 檢核表、比對矩陣，以及【附錄：引證前案原文摘錄】。")

        with col_dl2:
            st.download_button(
                "📊 下載前案比對矩陣 (.csv)",
                data=csv_bytes,
                file_name=f"claim_chart_{time_str}.csv",
                mime="text/csv",
                type="secondary",
                use_container_width=True
            )
            st.caption("格式：標準 UTF-8 BOM CSV，適合 Excel / 試算表直接編輯與建檔。")

# ==============================================================================
# TAB 2: 商標權模組 (整合 Logo 上傳圖文合成)
# ==============================================================================
with tab_trademark:
    st.subheader("🏷️ 商標尼斯分類佈局與 TIPO 規範圖樣產生器")
    st.markdown("評估商標識別性（Distinctiveness）、自動推薦第 09/42 類商品，並支援上傳 Logo 圖片合成符合智財局規範之申請圖檔。")

    if "tm_analysis" not in st.session_state:
        st.session_state.tm_analysis = None

    col_tm1, col_tm2 = st.columns([1, 1])

    with col_tm1:
        st.markdown("#### 1. 品牌標的與產品資訊")
        tm_brand = st.text_input("擬申請商標文字 (中/英文)：", value="葉語 SpectrIQ", placeholder="例如：葉語 SpectrIQ 或 SpectrIQ")
        tm_desc = st.text_area("產品或服務技術概述：", value="基於多光譜感測與邊緣 AI 推論之溫室作物病害早期預警系統，包含感測儀器硬體與雲端 SaaS 分析平台。", height=100)

        if st.button("✨ 執行商標識別性與尼斯分類 AI 評估", type="secondary", use_container_width=True, key="btn_tm_ai"):
            if not user_api_key.strip():
                st.error("請先於左側側邊欄輸入有效的 Gemini API Key！")
            elif not tm_brand.strip():
                st.warning("請填寫擬申請之商標文字。")
            else:
                with st.spinner("🤖 正在調用 Gemini 評估商標識別性與分類..."):
                    try:
                        st.session_state.tm_analysis = analyze_trademark_with_gemini(user_api_key.strip(), tm_brand.strip(), tm_desc.strip())
                        st.success("🎉 商標分析完成！")
                    except Exception as e:
                        st.error(f"分析失敗: {e}")

        if st.session_state.tm_analysis:
            res = st.session_state.tm_analysis
            st.markdown("---")
            st.markdown("#### 📋 智財審查可行性分析")
            st.info(f"**識別性等級判定**：{res.get('distinctiveness_level', '未知')}\n\n**審查風險備註**：{res.get('legal_risk_analysis', '')}")

            st.markdown("#### 📦 推薦指定之尼斯分類與標準項目")
            for cls in res.get("nice_classes", []):
                with st.expander(f"📌 {cls.get('class_num')} (類似組群碼: {cls.get('group_codes')})", expanded=True):
                    st.write(f"**建議指定商品/服務項目**：\n{cls.get('recommended_items')}")

            st.markdown("#### 🔍 TIPO 官方前案檢索建議關鍵字")
            st.code(res.get("clearance_search_keywords", ""), language="text")
            st.link_button("🇹🇼 開啟經濟部智慧局商標檢索首頁", "https://twtmsearch.tipo.gov.tw/", use_container_width=True)

    with col_tm2:
        st.markdown("#### 2. TIPO 電子送件商標圖樣即時產生器 (含 Logo 合成)")
        st.caption("官方硬性規範：8×8 公分、300 DPI、945×945 px、純白底色、RGB 模式 JPEG。")

        uploaded_logo = st.file_uploader("選填：上傳品牌 Logo 圖檔 (支援 PNG、JPG，透明底自動填白)", type=["png", "jpg", "jpeg"])

        layout_options = ["純文字：單行水平置中", "純文字：上下雙行置中"]
        if uploaded_logo is not None:
            layout_options = ["複合商標：上圖下文", "複合商標：左圖右文"] + layout_options

        layout_choice = st.selectbox("圖樣排版方式：", layout_options)
        font_size_val = st.slider("文字字級大小 (Font Size)：", min_value=36, max_value=120, value=64 if uploaded_logo else 76, step=2)

        if tm_brand.strip():
            img_bytes = create_tipo_trademark_bytes(
                text=tm_brand.strip(),
                layout=layout_choice,
                font_size=font_size_val,
                logo_file=uploaded_logo
            )
            st.image(img_bytes, caption="📸 圖樣預覽 (8x8 cm @ 300 DPI 標準白底)", width=320)

            clean_filename = f"trademark_{tm_brand.strip().replace(' ', '_')}.jpg"
            st.download_button(
                label="📥 下載標準商標圖樣檔 (.jpg)",
                data=img_bytes,
                file_name=clean_filename,
                mime="image/jpeg",
                type="primary",
                use_container_width=True
            )
            st.caption("💡 說明：此 JPG 圖檔已完全符合智慧局 E-filing 送件系統規格，可直接作為註冊圖樣上傳。")
        else:
            st.warning("請先於左側輸入商標名稱以生成圖樣。")

# ==============================================================================
# TAB 3: 智財法規速查模組 (專利法 ＆ 商標法)
# ==============================================================================
with tab_laws:
    st.subheader("⚖️ 專利法與商標法關鍵條文速查指南")
    st.markdown("快速檢索與參考台灣**《專利法》**與**《商標法》**核心條文、實務審查要點與常見核駁/答辯條款。")

    col_filter1, col_filter2 = st.columns([1, 2])
    with col_filter1:
        law_type_filter = st.selectbox("篩選法規類別：", ["全部法規", "專利法", "商標法"])
    with col_filter2:
        search_kw = st.text_input("輸入條文、標題或關鍵字快速過濾：", placeholder="例如：新穎性、進步性、混淆誤認、識別性、排他權")

    # 執行過濾篩選
    filtered_laws = IP_LAWS_DB
    if law_type_filter != "全部法規":
        filtered_laws = [item for item in filtered_laws if item["category"] == law_type_filter]

    if search_kw.strip():
        kw = search_kw.strip().lower()
        filtered_laws = [
            item for item in filtered_laws
            if kw in item["article"].lower() or kw in item["title"].lower() or kw in item["keywords"].lower() or kw in item["text"].lower()
        ]

    st.caption(f"共找到 {len(filtered_laws)} 則相關核心法規條文：")

    for item in filtered_laws:
        badge = "📄 專利法" if item["category"] == "專利法" else "🏷️ 商標法"
        expander_title = f"{badge} ｜ {item['article']}：{item['title']}"
        with st.expander(expander_title, expanded=True if search_kw.strip() else False):
            st.markdown(f"**🔍 關鍵字標籤**：`{item['keywords']}`")
            st.markdown("##### 📜 法定條文內容：")
            st.code(item["text"], language="text")
            st.markdown("##### 💡 審查實務與答辯要點：")
            st.info(item["explanation"])

    st.markdown("---")
    st.markdown("#### 🌐 官方全國法規資料庫即時連結")
    col_ext1, col_ext2, col_ext3 = st.columns(3)
    with col_ext1:
        st.link_button("📜 中華民國《專利法》完整法條", "https://law.moj.gov.tw/LawClass/LawAll.aspx?pcode=J0070007", use_container_width=True)
    with col_ext2:
        st.link_button("🏷️ 中華民國《商標法》完整法條", "https://law.moj.gov.tw/LawClass/LawAll.aspx?pcode=J0070001", use_container_width=True)
    with col_ext3:
        st.link_button("🏛️ 智慧財產局專利/商標審查基準", "https://www.tipo.gov.tw/", use_container_width=True)
