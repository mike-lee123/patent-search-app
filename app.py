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
# 一、 核心資料結構與專利檢索邏輯 (已修正 Google Patents 官方標準語法)
# ==============================================================================
@dataclass
class TechnicalPillar:
    """定義單一技術支柱（名稱、英文關鍵字、中文關鍵字）"""
    name: str
    en_keywords: List[str] = field(default_factory=list)
    zh_keywords: List[str] = field(default_factory=list)

class PatentSearchBuilder:
    """專利檢索邏輯式建造器 (含 Google Patents 扁平化防禦與官方分類號相容規範)"""
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
        """產生符合 Google Patents 官方解析器標準之檢索式 (無重複前綴、無分號、單層扁平)"""
        pillar_blocks = []
        for p in self.pillars:
            if p.en_keywords:
                selected_kws = p.en_keywords[:3]
                formatted = [f'"{kw}"' if " " in kw else kw for kw in selected_kws]
                pillar_blocks.append(f"({' OR '.join(formatted)})")

        keyword_part = " AND ".join(pillar_blocks) if pillar_blocks else ""

        all_classes = self.cpc_classes or self.ipc_classes
        if all_classes:
            clean_classes = []
            for c in all_classes:
                raw_c = re.sub(r'\s+', '', c).strip().upper()
                if raw_c:
                    clean_classes.append(raw_c)
            
            if clean_classes:
                classes_str = f"({' OR '.join(clean_classes)})"
                if keyword_part:
                    return f"{keyword_part} AND {classes_str}"
                return classes_str

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
# 二、 專利號爬取與內容解析 (已修復 UTF-8 中文解碼與語言路由)
# ==============================================================================
def fetch_patent_data_from_google(patent_no: str) -> dict:
    """從 Google Patents 爬取專利資料 (嚴格以 UTF-8 解碼，杜絕繁簡中文亂碼)"""
    clean_pno = re.sub(r'[\s\-_/]', '', patent_no).upper()
    
    # 智慧語言路由：CN 或 TW 專利優先存取原始中文介面，其餘導向 /en 英文介面
    if clean_pno.startswith(("CN", "TW")):
        url = f"https://patents.google.com/patent/{clean_pno}"
    else:
        url = f"https://patents.google.com/patent/{clean_pno}/en"

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    resp = requests.get(url, headers=headers, timeout=12)
    if resp.status_code != 200:
        raise Exception(f"無法取得專利資料 (HTTP {resp.status_code})，請確認專利號碼是否正確。")

    # 強制指定 UTF-8，杜絕 requests 誤判為 ISO-8859-1
    resp.encoding = 'utf-8'

    # 使用二進位 content 搭配明確編碼解析
    soup = BeautifulSoup(resp.content, "html.parser", from_encoding="utf-8")

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
# 三、 Gemini AI 自動重試與急速輪替機制 (防禦 503 UNAVAILABLE 與 429 尖峰)
# ==============================================================================
CANDIDATE_MODELS = [
    "gemini-2.5-flash",
    "gemini-3.5-flash",
    "gemini-3.6-flash",
    "gemini-2.5-pro",
    "gemini-3.8-flash"
]

def generate_with_fallback(client, prompt: str, as_json: bool = True) -> str:
    """自動跨多款模型急速輪替，遭遇 503/429 立即切換下一款備援模型"""
    last_exception = None
    config_args = {"response_mime_type": "application/json"} if as_json else {}

    for model_name in CANDIDATE_MODELS:
        for attempt in range(2):
            try:
                response = client.models.generate_content(
                    model=model_name,
                    contents=prompt,
                    config=types.GenerateContentConfig(**config_args)
                )
                if response and response.text:
                    return response.text
            except Exception as e:
                last_exception = e
                err_str = str(e)
                if any(code in err_str for code in ["503", "429", "UNAVAILABLE", "RESOURCE_EXHAUSTED"]):
                    time.sleep(1.2 * (attempt + 1))
                    break  # 跳出當前模型重試，換下一款模型
                if "404" in err_str or "NOT_FOUND" in err_str:
                    break
                break

    raise last_exception if last_exception else Exception("所有備援模型皆忙碌或暫時不可用，請稍後重試。")

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
    res_text = generate_with_fallback(client, prompt, as_json=True)
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
    res_text = generate_with_fallback(client, prompt, as_json=True)
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
    res_text = generate_with_fallback(client, prompt, as_json=True)
    return json.loads(res_text)

def generate_oa_response_with_gemini(api_key: str, law_article: str, target_name: str, rejection_grounds: str, diff_facts: str) -> str:
    """自動撰寫智財局核駁審查意見申復理由書草稿"""
    client = genai.Client(api_key=api_key)
    prompt = f"""
    你是一名台灣資深專利代理人與商標代理人。
    請依據台灣《經濟部智慧財產局（TIPO）》官方審查基準與法定申復書規格，針對下列核駁審查意見通知函（Office Action）或爭議指控，撰寫一份結構嚴謹、條理分明、具高度法理說服力的【申復/答辯理由書（草稿）】。

    【引用法條/爭議事由】：{law_article}
    【本案標的名稱/對造商標】：{target_name}
    【核駁或指控理由摘要】：{rejection_grounds}
    【申請人/答辯人主張之實體差異事實與論據】：{diff_facts}

    請使用正式專利/商標法律繁體中文撰寫，內容必須包含以下章節架構：
    一、案由與前言聲明（明確載明答辯標的與訴求）
    二、法規意旨與審查基準法理依據（引述該法條立法意旨與智慧局審查基準）
    三、爭點具體比對與實體答辯理由：
        1. 技術特徵/商品性質、功能用途、材料領域之顯著區隔
        2. 克服核駁或侵權要件之核心論據（若涉及商標，需著重於商品非類似、尼斯分類分流、專業購買者注意程度、產製主體無跨界通念等；若涉及專利，需著重於非顯而易知性與協同功效）
    四、結論與懇請事項（懇請審查官/主管機關准予核准審定 / 駁回異議訴求）

    請直接輸出完整排版格式的申復答辯理由書。
    """
    return generate_with_fallback(client, prompt, as_json=False)

# ==============================================================================
# 四、 商標圖樣繪製核心邏輯
# ==============================================================================
def create_tipo_trademark_bytes(
    text: str,
    layout: str = "純文字模式",
    font_size: int = 68,
    text_align: str = "置中對齊",
    line_spacing_ratio: float = 0.35,
    logo_file=None
) -> bytes:
    """產生符合 TIPO 電子送件 8x8 cm 300DPI (945x945 px) 規格之 JPEG bytes"""
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

    lines = [line.strip() for line in text.strip().split("\n") if line.strip()]
    if not lines:
        lines = [""]

    line_bboxes = []
    line_widths = []
    line_heights = []
    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font)
        lw = bbox[2] - bbox[0]
        lh = bbox[3] - bbox[1]
        line_bboxes.append(bbox)
        line_widths.append(lw)
        line_heights.append(lh)

    line_spacing_px = int(font_size * line_spacing_ratio)
    total_text_h = sum(line_heights) + line_spacing_px * (len(lines) - 1)
    max_line_w = max(line_widths) if line_widths else 0

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

    def draw_multiline_block(start_top_y: int, block_center_x: int, block_w: int):
        cur_y = start_top_y
        for i, line in enumerate(lines):
            lw = line_widths[i]
            lh = line_heights[i]
            bbox = line_bboxes[i]

            if text_align == "靠左對齊":
                tx = block_center_x - (block_w // 2) - bbox[0]
            elif text_align == "靠右對齊":
                tx = block_center_x + (block_w // 2) - lw - bbox[0]
            else:
                tx = block_center_x - (lw // 2) - bbox[0]

            draw.text((tx, cur_y - bbox[1]), line, font=font, fill=(0, 0, 0))
            cur_y += lh + line_spacing_px

    if logo_img and layout == "複合商標：上圖下文":
        target_logo_h = int(height_px * 0.42)
        aspect = logo_img.width / logo_img.height
        new_w = int(target_logo_h * aspect)
        if new_w > int(width_px * 0.75):
            new_w = int(width_px * 0.75)
            target_logo_h = int(new_w / aspect)
        resized_logo = logo_img.resize((new_w, target_logo_h), Image.Resampling.LANCZOS)

        spacing = int(height_px * 0.04)
        total_block_h = target_logo_h + spacing + total_text_h
        start_y = (height_px - total_block_h) // 2

        logo_x = (width_px - new_w) // 2
        canvas.paste(resized_logo, (logo_x, start_y))

        text_start_y = start_y + target_logo_h + spacing
        draw_multiline_block(text_start_y, width_px // 2, max_line_w)

    elif logo_img and layout == "複合商標：左圖右文":
        target_logo_w = int(width_px * 0.35)
        aspect = logo_img.height / logo_img.width
        new_h = int(target_logo_w * aspect)
        if new_h > int(height_px * 0.6):
            new_h = int(height_px * 0.6)
            target_logo_w = int(new_h / aspect)
        resized_logo = logo_img.resize((target_logo_w, new_h), Image.Resampling.LANCZOS)

        spacing = int(width_px * 0.04)
        total_block_w = target_logo_w + spacing + max_line_w
        start_x = (width_px - total_block_w) // 2

        logo_y = (height_px - new_h) // 2
        canvas.paste(resized_logo, (logo_x, logo_y))

        text_center_x = start_x + target_logo_w + spacing + (max_line_w // 2)
        text_start_y = (height_px - total_text_h) // 2
        draw_multiline_block(text_start_y, text_center_x, max_line_w)

    else:
        start_y = (height_px - total_text_h) // 2
        draw_multiline_block(start_y, width_px // 2, max_line_w)

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
# 六、 智財核心法規與法律範本資料庫
# ==============================================================================
IP_LAWS_DB = [
    # 專利法核心
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
        "title": "專利權效力之限制（合理使用與先使用權）",
        "keywords": "效力限制, 非營利, 研究, 試驗, 藥品查驗, 先使用權",
        "text": (
            "發明專利權之效力，不及於下列各款情事：\n"
            "一、非出於商業目的之未公開行為。\n"
            "二、以研究或實驗為目的實施發明之必要行為。\n"
            "三、在專利申請日前，在國內已實施該發明，或已完成必須之準備者（先使用權）。"
        ),
        "explanation": "若配方保留為營業秘密，在他人申請日前已在國內量產或完成準備，得依第3款主張先使用權，但僅限於原事業目的與規模內繼續實施。"
    },

    # 化學配方專利專題
    {
        "category": "化學配方專利專題",
        "article": "專利法 第 22 條 審查基準",
        "title": "化學組成物配方之進步性判定（協同效應）",
        "keywords": "化學配方, 協同效應, Synergistic Effect, 突變性增益, 數值限定",
        "text": (
            "化學組成物若由已知成分混合而成，原則上視為先前技術之通常替換。\n"
            "惟若特定配比範圍內能產生「協同效應（Synergistic Effect）」或「無法預期之技術功效（Unexpected Results）」，"
            "且非通常知識者依既有理論所能預測者，應認定具備進步性。"
        ),
        "explanation": "【實務防禦】化學配方答辯核駁時，必須提出說明書實施例數據，證明 A+B 在特定比例下之功效顯著大於各成分單獨效果相加（如電位大幅負移、抗蝕壽命倍增）。"
    },
    {
        "category": "化學配方專利專題",
        "article": "專利法 第 26 條 審查基準",
        "title": "化學配方可據以實現要件與實施例揭露要求",
        "keywords": "可據以實現, Enablement, 實施例, 比較例, 隱藏配方",
        "text": (
            "化學發明說明書應載明具體之製備實施例及物性確認數據，使同業無須過度過度實驗即可再現該發明。\n"
            "若申請人為保留商業秘密而隱匿關鍵催化劑、反應條件或添加順序，致使無法達到預期功效者，構成違反第26條第1項。"
        ),
        "explanation": "【專利 vs. 秘密抉擇】申請專利必須完全揭露「實施例」與「比較例」數據。若不想讓全世界看見真實調配參數，應審慎評估改走《營業秘密法》保護。"
    },

    # 營業秘密法核心
    {
        "category": "營業秘密法",
        "article": "營業秘密法 第 2 條",
        "title": "營業秘密之法定三要件",
        "keywords": "營業秘密, 秘密性, 經濟價值, 合理保密措施, 三要件",
        "text": (
            "本法所稱營業秘密，指方法、技術、製程、配方、程式、設計或其他可用於生產、銷售或經營之資訊，而符合下列要件者：\n"
            "一、非一般涉及該類資訊之人所知者（非知悉性 / 秘密性）。\n"
            "二、因其秘密性而具有實際或潛在之經濟價值者（經濟價值性）。\n"
            "三、所有人已採取合理之保密措施者（合理保密措施）。"
        ),
        "explanation": "【訴訟最關鍵點】法院判定配方是否受保護，核心在於第3款「合理保密措施」。工廠必須落實進料去識別化、虛擬料號、分段調配、門禁管制及簽署 NDA，否則將喪失秘密資格。"
    },
    {
        "category": "營業秘密法",
        "article": "營業秘密法 第 10 條",
        "title": "侵害營業秘密之行為態樣與民事救濟",
        "keywords": "侵權態樣, 竊取, 洩漏, 排除侵害, 懲罰性賠償",
        "text": (
            "有下列情形之一者，為侵害營業秘密：\n"
            "一、以不正當方法取得營業秘密者。\n"
            "二、知悉或因重大過失而不知其為前款之營業秘密，而取得、使用或洩漏者。\n"
            "三、取得營業秘密後，知悉或因重大過失而不知其為第一款之營業秘密，而使用或洩漏者。\n"
            "四、因法律行為取得營業秘密，而以不正當方法使用或洩漏者。"
        ),
        "explanation": "受侵害人得依第11條向法院請求排除侵害（查扣侵權品、禁令），並得依第12條請求損害賠償；若屬故意侵害，法院得酌定高達損害額三倍之懲罰性賠償金。"
    },
    {
        "category": "營業秘密法",
        "article": "營業秘密法 第 13 條之 1",
        "title": "侵害營業秘密之刑事責任（境內洩密罪）",
        "keywords": "刑事責任, 刑責, 竊取, 五年以下有期徒刑, 罰金",
        "text": (
            "意圖為自己或第三人不法之利益，或損害營業秘密所有人之利益，而有下列情形之一，處五年以下有期徒刑或拘役，得併科新臺幣一百萬元以上一千萬元以下罰金：\n"
            "一、以竊取、毀損、隱匿、詐術、脅迫、翻拍、複製或其他不正方法取得營業秘密，或取得後使用、洩漏者。\n"
            "二、知悉或持有營業秘密，未經授權或逾越授權範圍而重製、使用或洩漏者。"
        ),
        "explanation": "離職員工未經授權擅自拷貝配方表、帶走實驗記錄本或洩漏給新公司，即構成非告訴乃論或重大刑事公訴罪，面臨最高 5 年徒刑。"
    },
    {
        "category": "營業秘密法",
        "article": "營業秘密法 第 13 條之 2",
        "title": "意圖在境外使用罪（加重刑責）",
        "keywords": "境外使用罪, 域外管轄, 外國, 大陸地區, 十年以下有期徒刑",
        "text": (
            "意圖在外國、大陸地區、香港或澳門使用，而犯前條第一項各款之罪者，處一年以上十年以下有期徒刑，得併科新臺幣三百萬元以上五千萬元以下罰金。\n"
            "前項之未遂犯罰之。"
        ),
        "explanation": "【重度刑責】若意圖帶往海外、中國大陸等地設廠實施或交付對手，刑度跳升為 1 年以上 10 年以下有期徒刑，罰金最高達 5,000 萬元，且處罰未遂犯。"
    },

    # 商標法核心
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
        "explanation": "直接描述產品功能（如在水果賣場註冊「鮮甜可口」）欠缺先天識別性；但若經長期大規模商業行銷使公眾認知其為品牌，可主張後天識別性取得註冊。"
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
        "explanation": "商標核駁最常見條款。審查時會考量圖樣近似程度、商品類似程度與消費者注意程度等多元因素。"
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
        "explanation": "著名商標享跨類別擴張保護。即使商品或服務類別不相同，若使用他人著名品牌容易造成稀釋或減損信譽者，同樣不得註冊。"
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
        "explanation": "於同類商品使用相同商標屬直接侵權；於類似商品或使用近似商標，則以「致相關消費者混淆誤認之虞」為實質侵權成立之核心判定。"
    }
]

USER_MANUAL_MARKDOWN = """# 📖 智慧財產權整合工作台 操作手冊

---

## 🚀 準備作業：設定 API 金鑰
1. 開啟工作台首頁，查看螢幕**左側側邊欄（Sidebar）**。
2. 在 **「🔑 Gemini API 設定」** 欄位貼入您的 Google Gemini API Key。
3. （可選）若本機有設定 `.streamlit/secrets.toml`，系統會自動載入，不需重複輸入。

---

## 模組一：📄 專利檢索與 Claims 比對矩陣

### 步驟 1：發明標的與技術三支柱拆解
* **方式 A：使用內建範本（最快）**
  * 在左側側邊欄的 **「📁 專利技術範本」** 下拉選單中選取範本（例如：貴金屬電鍍光澤劑、多光譜溫室作物病害早期偵測系統），系統會自動帶入標的名稱、IPC/CPC、三支柱關鍵字及 Claims 要件。
* **方式 B：AI 自動拆解**
  1. 輸入發明標的名稱（例如：`晶圓搬運機械手臂動態抑振控制系統`）。
  2. 點擊 **「✨ Gemini AI 自動拆解」**。
  3. 系統自動產出 IPC/CPC、三支柱（Target / Mechanism / Effect）中英文關鍵字與 Claim 1 要件。
* **方式 C：手動編輯微調**
  * 各欄位關鍵字請使用半形逗號 `,` 隔開，包含空格的英文片語會自動以雙引號保護。

### 步驟 2：引證前案爬取與全要件比對 (Auto-fetch Prior Art)
1. **輸入前案專利號**：填入公開號或公告號（例如：`US11578418B2`、`CN110016700A`、`US8608931B2` 或 `EP3739504A1`）。
2. **選取填入欄位**：下拉選擇 `前案 D1 對應技術` 或 `前案 D2 對應技術`。
3. **點擊「📥 爬取並自動填入」**：
   * 系統自動爬取 Google Patents 摘要與 Claims 原文（已完整修復繁簡中文 UTF-8 編碼與語言路由，絕不亂碼）。
   * AI 自動將前案構件對應至 Claim 1 各 Element，並更新符合性判定（YES / NO / 均等成立）。
   * 內建防禦 503 / 429 機制，遇尖峰負載將自動毫秒級輪替備援模型。
4. **檢視原文**：可在下方展開卡片中即時閱讀摘要與 Claims 原文。

### 步驟 3：全要件矩陣線上編輯與進步性答辯
1. 矩陣支援線上即時編輯構件描述、符合性判定與差異功效。
2. **一鍵進步性申復**：當表格有判定為 `NO (不符/差異點)`，點擊 **「⚖️ 一鍵生成《專利法》第22條進步性申復理由」**，AI 自動提煉差異點起草申復書。

### 步驟 4：匯出檢索式與完整分析報告
1. 點擊 **「🚀 生成專利檢索式並整合比對報告」**。
2. 系統採用**扁平化防禦語法**，可直接一鍵複製或點擊前往 Google Patents 執行高精確檢索，絕不報錯。
3. 可下載完整報告檔 (`.txt`) 或 Claims 比對矩陣試算表 (`.csv`)。

---

## 模組二：🏷️ 商標權佈局與圖樣生成器

### 步驟 1：商標識別性與尼斯分類 AI 評估
1. 輸入「擬申請商標文字」與「產品/技術描述」。
2. 點擊 **「✨ 執行商標識別性與尼斯分類 AI 評估」**。
3. 取得識別性等級（獨創/任意/暗示/說明）、推薦尼斯分類組群與 TIPO 檢索關鍵字。

### 步驟 2：產生符合 TIPO 規範之商標圖檔 (含 Logo 合成)
1. **多行文字**：文字框支援直接按下 Enter 自由換行。
2. **Logo 上傳**：支援 PNG/JPG 圖檔，透明底自動填白。
3. **版面控制**：支援純文字/上圖下文/左圖右文，具備靠左/置中/靠右對齊與行距倍率調整滑桿。
4. **規格保證**：輸出符合官方 E-filing 規範之 8×8 cm @ 300 DPI（945×945 px）純白底色 JPEG。

---

## 模組三：⚖️ 智財法規速查 ＆ AI 申復答辯理由書產生器

### 步驟 1：條文與審查實務速查
* 依類別篩選專利法、商標法、營業秘密法或化學配方專題，或以關鍵字（進步性、協同效應、合理保密措施、刑責）即時過濾核心條文與答辯要點。

### 步驟 2：AI 申復答辯理由書產生器
1. 選取法定條款範本（含：化學配方進步性協同增效、說明書充分揭露、商品非類似抗辯等）。
2. 系統自動帶入專屬之爭點事實與實體論據。
3. 點擊 **「✨ 產生申復答辯理由書草稿」**，產出符合官方格式之正式理由書，支援一鍵複製與 txt 下載。
"""

TRADE_SECRET_AGREEMENT_DOC = """營業秘密保密暨離職切結書

立切結書人：＿＿＿＿＿＿＿＿＿＿（以下簡稱「乙方」）
身分證統一編號：＿＿＿＿＿＿＿＿＿＿
原任職部門／職稱：＿＿＿＿＿＿＿＿＿＿／＿＿＿＿＿＿＿＿＿＿
離職生效日期：中華民國＿＿＿年＿＿＿月＿＿＿日

緣乙方原受僱於＿＿＿＿＿＿＿＿＿＿股份有限公司（以下簡稱「甲方」），於任職期間因職務需要，知悉、接觸或取得甲方之各項機密技術與商業資訊。現因乙方於上述生效日終止與甲方之勞動契約，為釐清權益並恪遵法律規範，特立此切結書，承諾並恪遵下列條款：

--------------------------------------------------------------------------------
第一條：營業秘密之具體範圍與標的
--------------------------------------------------------------------------------
乙方明確知悉並承認，其於任職期間所接觸、知悉或產生之下列資訊，無論其形式為書面、電子檔案、口頭、實體物件或樣品，均屬《營業秘密法》第二條所保護之甲方核心營業秘密：
1. 配方與物化技術參數：
   - 包含但不限於特種塗料、表面處理劑、電鍍光澤劑／細化劑、黏著劑等之組成物原料配比、各成分重量份／百分比。
   - 關鍵添加劑之特定分子結構、合成路徑、反應動力學曲線、流變性數據、電位極化參數等。
2. 製程、投料與加料工藝：
   - 包含但不限於特定反應溫度、真空度、剪切攪拌轉速、熟化時間、加料先後順序、母液預混調配方法等實務工藝。
3. 去識別化對照機制與供應鏈資訊：
   - 甲方內部專屬之原料虛擬編號、代號對照表（例如：R-代碼對應之真實化學名或供應商 CAS 號碼）。
   - 特殊關鍵原料之獨家供應商名單、議價條件、進貨成本及配方客戶客製化規格要求。
4. 研發日誌與實驗未公開成果：
   - 包含研發紀錄本（Lab Notebook）、未公開之實施例與比較例數據、未送件或審查中之專利初稿及失效專利評估報告。

--------------------------------------------------------------------------------
第二條：保密義務與不作為承諾
--------------------------------------------------------------------------------
1. 嚴格保密：乙方承諾自離職日起，非經甲方事前書面同意，絕不以口頭、書面、電子傳輸、影印、翻拍、社群網路、口傳或其他任何方式，洩漏、交付、公開、發表或以任何手段使任何第三人知悉前條所述之營業秘密。
2. 禁止不法使用：乙方承諾絕不為自己或任何第三人（包括但不限於乙方未來任職、投資、合夥、顧問或實質控制之新公司、競爭對手）之利益，實施、調配、試驗、複製、利用或參考前條所述之任何配方或製程技術。
3. 終身保密期限：第一條所列之營業秘密，於其依法公開成為該領域通常知識前，乙方之保密義務不因勞動契約終止、離職時間久暫或任何身分變更而消滅。

--------------------------------------------------------------------------------
第三條：公物、資料及權限全數返還與結清切結
--------------------------------------------------------------------------------
乙方特此聲明並保證，於離職手續辦理完竣前，已確實履行下列義務：
1. 實體物件清點返還：所有載有或涉及甲方營業秘密之實體文件、工作手冊、配方卡、調配紀錄單、實驗筆記、樣品、原料試劑、門禁卡及識別證，均已全數當面點交歸還甲方，絕無藏匿、轉載或扣留任何複本或抄本。
2. 數位資料全數刪除與移交：所有存儲於公司配發或乙方個人之電腦、隨身碟、外接硬碟、個人雲端空間（如 Google Drive、Dropbox 等）、私人電子信箱、通訊軟體群組內之機密檔案、代號表、實驗照片，均已全數移交甲方主管並自個人設備徹底刪除，絕未備份、轉傳或上傳至未授權端點。
3. 數位軌跡切結：乙方同意甲方得於離職前或離職後合理期間內，針對公司指派之公務電腦及公務信箱進行必要之資訊安全檢視與日誌（Log）稽核，以確認資料交接之完整性。

--------------------------------------------------------------------------------
第四條：法律責任特別告知與刑事法令警示
--------------------------------------------------------------------------------
乙方簽署本切結書時，已獲甲方指派專人明確告知並充分理解相關法律規範，乙方知悉如有違反應負下列法律責任：
1. 民事賠償責任：
   - 依《營業秘密法》第十一條，甲方得向法院聲請假處分、定暫時狀態處分，禁止乙方實施或洩漏，並得請求排除侵害及銷毀侵權物品。
   - 依《營業秘密法》第十二條及第十三條，乙方除應賠償甲方因此所受之一切損害（包含所受損害、所失利益及律師費用）外，若屬故意侵害，法院得酌定高達損害額三倍之懲罰性賠償金。
2. 刑事責任（國內洩密罪）：
   - 依《營業秘密法》第十三條之一，意圖為自己或第三人不法之利益，或損害營業秘密所有人之利益，以竊取、毀損、隱匿、未經授權重製或取得，或知悉後擅自使用、洩漏者，處五年以下有期徒刑或拘役，得併科新臺幣一百萬元以上一千萬元以下罰金。
3. 重度刑事責任（境外洩密罪）：
   - 依《營業秘密法》第十三條之二，意圖在外國、大陸地區、香港或澳門使用而犯前條之罪者，處一年以上十年以下有期徒刑，得併科新臺幣三百萬元以上五千萬元以下罰金。

--------------------------------------------------------------------------------
第五條：管轄法院與其他約定
--------------------------------------------------------------------------------
1. 準據法與合意管轄：本切結書之解釋與適用，悉依中華民國法律。因本切結書所生之爭議或涉訟時，雙方合意以臺灣智慧財產及商業法院為第一審管轄法院。
2. 可分性原則：本切結書任一條款如經法院裁判有無效或不可執行者，其他條款之效力不受影響，仍具完全法定約束力。
3. 簽署生效：本切結書一式兩份，由甲、乙雙方各執一份為憑，自乙方簽署之日起即時生效。

立切結書人（乙方）簽章：＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿
戶籍地址：＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿
現居地址：＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿
聯絡電話／手機：＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿

見證人（甲方主管／人資代表）簽章：＿＿＿＿＿＿＿＿＿＿＿

中華民國    年    月    日
"""

OA_ELECTROPLATING_DOC = """專利申復理由書（草稿）

案  號：第 [請填入申請案號] 號
申 請 人：[請填入專利申請人/公司名稱]
發明名稱：用於貴金屬電鍍之晶粒細化光澤添加劑組成物
受 文 者：經濟部智慧財產局

--------------------------------------------------------------------------------
一、 案由與前言聲明
--------------------------------------------------------------------------------
本件專利申請案業經 貴局審查官惠示審查意見通知函，認本案申請專利範圍請求項第 1 項等技術特徵，為所屬技術領域中具有通常知識者結合引證案 D1 與引證案 D2 所能輕易置換思及完成，而有違反《專利法》第 22 條第 2 項（進步性）之虞。

申請人深感審查官審查之辛勞，經詳加研析前揭核駁理由與引證文獻後，謹陳明：引證案 D1 與引證案 D2 實質上並未揭露本案請求項第 1 項所特定界定之「主光澤劑與輔助細化劑之重量比為 1:1 至 10:1」之關鍵吸附平衡技術特徵（Element 1C），更未教示或暗示該特定配比能誘發「陰極極化過電位負移 50 至 200 mV」並將晶粒強制細化至 80 nm 以下且杜絕脆化之突變性協同增效（Synergistic Effect）。本案確實非通常知識者依先前技術所能輕易完成，具備突出之技術特徵與顯著之功效增益，依法自具進步性。

茲檢具實體法律與技術比對理由如后，懇請 貴局審查官明察並賜予核准審定。

--------------------------------------------------------------------------------
二、 審查基準法理依據
--------------------------------------------------------------------------------
按《專利法》第 22 條第 2 項規定，發明雖無同條第 1 項各款所列情事，但為其所屬技術領域中具有通常知識者依申請前之先前技術所能輕易完成時，仍不得依本法申請專利。

次按 貴局頒布之《專利審查基準》第二篇第三章第 3.4 節「進步性之判斷」明載：
1. 「不可事後諸葛（Avoid Hindsight Bias）」：判斷進步性時，審查人員不得於已知本發明內容之情況下，主觀推斷或重組先前技術元件。先前技術若未提供結合之「動機或啟示（Teaching, Suggestion, or Motivation）」，即不得任意將多份引證案拼湊以否定進步性。
2. 「無法預期之技術功效（Unexpected Technical Effect）」：在數值範圍或成分配比之發明中，若發明限定之特定成分比例範圍，於臨界區間內產生了超越各成分單純功效相加、非通常知識者依既有理論所能預測之突變性增益或協同效果者，即應認定具備進步性。

--------------------------------------------------------------------------------
三、 爭點具體比對與實體答辯理由
--------------------------------------------------------------------------------
本案請求項第 1 項之核心技術要件與引證案 D1、D2 之全要件比對結果如下：

（一） 引證案未曾揭露本案特定 1:1 至 10:1 之重量配比限制（Element 1C）
1. 引證案 D1 之揭露極限：
   引證案 D1 僅為一般有機添加劑之單純教示，其說明書通篇僅泛稱添加常規吡啶類衍生物作為光澤劑，並完全未限定該主光澤劑與含硫輔助成分之精確相互作用配比，實務上係由現場操作人員隨機視槽況目視補正，對配比動態平衡毫無實質教示。
2. 引證案 D2 之技術阻礙（Teaching Away）：
   引證案 D2 揭露之硫脲抑制體系，係採取極低量之微量抑制添加模式（其主添加劑與抑制劑之比例高達 1:20 以上）。引證案 D2 明白指出：若提高含硫添加劑之濃度至接近主光澤劑（如 1:1 至 1:10 之高濃度相對比值），將導致鍍層產生嚴重的共析脆化、內應力劇增與變色缺陷。
3. 兩者結合無法導出本案特徵：
   通常知識者參酌引證案 D2 之負面教示，理應竭力避免將兩者配比維持於 1:1 至 10:1 之高比例區間。因此，先前技術不僅缺乏將兩者以 1:1 至 10:1 配比結合之技術啟示，甚至存在強烈之反向教示。

（二） 本案特定數值配比產生無法預期之「動態競爭吸附與微晶協同功效」
本案發明人經反覆實驗突破性發現，當含氮芳香雜環/聚季銨鹽陽離子主光澤劑（成分 a）與含硫有機抑制劑（成分 b）之重量比被嚴格鎖定於 1:1 至 10:1 時，於陰極微觀雙電層（Helmholtz Layer）表面將引發不可預期的相乘作用：
1. 陰極極化過電位大幅負移 50 至 200 mV：
   單獨使用成分 (a)，電位負移量未達 20 mV；單獨使用成分 (b)，過電位雖有增長但極化曲線極不穩定，高電流密度區易發生析氫與燒焦。唯有當兩者維持於 1:1 至 10:1 之黃金配比時，陽離子季銨基與含硫硫醇/磺酸基團在陰極凸起處形成高密度的複合金屬錯合物吸附膜，促使過電位急遽負移 50～200 mV。
2. 晶粒形核速率（Nucleation Rate）呈指數級躍升：
   過電位之顯著負移，大幅提高了晶核生成能障，使貴金屬沉積機制由「平穩晶體長大」強制切換為「高密度連續均勻形核」。依據本案說明書實施例數據，本案鍍層晶粒尺寸被抑制於 80 奈米（nm）以下，表面粗糙度 Ra 降至 0.05 μm 以下，達到鏡面反射效果。
3. 消除硫原子夾雜，杜絕鍍層脆化：
   在 1:1 至 10:1 之交互作用下，成分 (a) 之立體阻礙效應調控了成分 (b) 的解離速率，徹底克服了先前技術（如引證案 D2）晶粒細化伴隨鍍層發脆的頑疾，使接點鍍層之打線結合力（Wire Bonding Pull Strength）提升 35% 以上，接觸阻抗維持低於 5 mΩ。

上述顯著之物理化學性質突變，絕非由引證案 D1 或 D2 任何單一組分所能預期，係屬典型的協同增效作用（Synergistic Effect），完全符合專利審查基準判定具備進步性之要件。

--------------------------------------------------------------------------------
四、 結論與懇請事項
--------------------------------------------------------------------------------
綜上所陳，本案申請專利範圍請求項第 1 項所請之技術方案，其所特定之成分配合比（Element 1C）不僅未見於引證案 D1 與 D2，更具備反技術常規之獨創性，且客觀上產生了先前技術所無法達成之顯著技術功效增益，實質上完全具備《專利法》第 22 條第 2 項規定之進步性要件。

懇請 貴局審查官明鍳上述事實與法理說明，惠予撤銷原核駁意見通知函之質疑，早日賜准本案專利，實感德便。

謹呈
經濟部智慧財產局 公鑒

申請人：[請填入申請人/專利代理人簽章]
日 期：中華民國 [請填入年/月/日]
"""

# ==============================================================================
# 七、 Streamlit 介面與 Session State 同步管理 (100% 保證自動填入)
# ==============================================================================
st.set_page_config(
    page_title="智慧財產權整合工作台 (專利 ＆ 商標)",
    page_icon="🛡️",
    layout="wide"
)

default_keys = {
    "patent_title_input": "",
    "ipc_input_val": "",
    "cpc_input_val": "",
    "p1_n_val": "Target: 應用標的",
    "p1_e_val": "",
    "p1_z_val": "",
    "p2_n_val": "Mechanism: 核心手段/機構",
    "p2_e_val": "",
    "p2_z_val": "",
    "p3_n_val": "Effect: 技術功效/特徵",
    "p3_e_val": "",
    "p3_z_val": "",
    "claims_data": [
        {"要件編號": "Element 1A", "本案 Claim 1 技術要件": "", "前案 D1 對應技術": "", "前案 D2 對應技術": "", "符合性判定": "待確認", "差異/進步性說明": ""}
    ],
    "last_fetched_patent": None,
    "tm_analysis": None,
    "last_oa_result": None
}

for k, v in default_keys.items():
    if k not in st.session_state:
        st.session_state[k] = v

st.title("🛡️ 智慧財產權整合工作台 (專利 ＆ 商標)")
st.markdown("結合 **Google Patents 邏輯檢索**、**專利號自動爬取對應**、**Claims 全要件比對矩陣**、**TIPO 規範圖樣生成** 與 **智財法規答辯生成器**。")

# ------------------------------------------------------------------------------
# 側邊欄：API 設定、操作手冊、營業秘密離職切結書常駐面板
# ------------------------------------------------------------------------------
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

# 📖 側邊欄操作手冊
with st.sidebar.expander("📖 操作手冊與使用說明", expanded=False):
    st.markdown(USER_MANUAL_MARKDOWN)
    st.download_button(
        label="📥 下載操作手冊 (.md)",
        data=USER_MANUAL_MARKDOWN,
        file_name="IP_Workbench_User_Manual.md",
        mime="text/markdown",
        use_container_width=True
    )

# 🔒 側邊欄：化學工廠專用營業秘密保密暨離職切結書
with st.sidebar.expander("🔒 營業秘密離職切結書範本", expanded=False):
    st.caption("依據《營業秘密法》第2條及第13條之1/之2擬定，具備具體標的清單與刑事責任警示。")
    trade_secret_edit = st.text_area(
        "切結書內容（可線上微調公司或員工資料）：",
        value=TRADE_SECRET_AGREEMENT_DOC,
        height=220,
        key="trade_secret_sidebar_area"
    )
    render_copy_button(trade_secret_edit, "📋 複製切結書全文", button_id="copyTradeSecretSidebar")
    cur_ts_time = datetime.now().strftime("%Y%m%d_%H%M%S")
    st.download_button(
        label="📥 下載離職切結書 (.txt)",
        data=trade_secret_edit.encode("utf-8"),
        file_name=f"Trade_Secret_NDA_{cur_ts_time}.txt",
        mime="text/plain;charset=utf-8",
        use_container_width=True
    )

tab_patent, tab_trademark, tab_laws = st.tabs([
    "📄 專利檢索與 Claims 比對矩陣",
    "🏷️ 商標權佈局與圖樣生成器",
    "⚖️ 智財法規速查 (專利法、商標法 ＆ 營業秘密法)"
])

# ==============================================================================
# TAB 1: 專利權模組
# ==============================================================================
with tab_patent:
    st.sidebar.markdown("---")
    st.sidebar.header("📁 專利技術範本")
    
    def apply_template():
        sel = st.session_state["template_select_key"]
        if sel == "貴金屬電鍍晶粒細化光澤劑":
            st.session_state["patent_title_input"] = "用於貴金屬電鍍之晶粒細化光澤添加劑組成物"
            st.session_state["ipc_input_val"] = "C25D 3/46, C25D 3/48, C25D 3/62, C25D 3/64"
            st.session_state["cpc_input_val"] = "C25D 3/46, C25D 3/48, C25D 3/64"
            st.session_state["p1_n_val"] = "Target: 貴金屬電鍍浴與接觸件"
            st.session_state["p1_e_val"] = "electroplating bath, gold electroplating, silver plating, contact terminal"
            st.session_state["p1_z_val"] = "電鍍浴, 鍍金, 鍍銀, 接觸端子, 引線框架, 貴金屬沉積"
            st.session_state["p2_n_val"] = "Mechanism: 雜環季銨鹽與含硫細化劑協同"
            st.session_state["p2_e_val"] = "grain refiner, brightener, quaternary ammonium, heterocyclic compound"
            st.session_state["p2_z_val"] = "晶粒細化劑, 光澤劑, 聚季銨鹽, 芳香雜環, 硫丙基二硫化物, 陰極極化"
            st.session_state["p3_n_val"] = "Effect: 奈米微晶緻密與耐磨抗氧化"
            st.session_state["p3_e_val"] = "nanocrystalline, dendritic suppression, low contact resistance, wear resistance"
            st.session_state["p3_z_val"] = "奈米晶粒, 抑制枝晶, 低接觸阻抗, 耐磨耗, 打線結合力, 鏡面光澤"
            st.session_state["claims_data"] = [
                {"要件編號": "Element 1A", "本案 Claim 1 技術要件": "一貴金屬電鍍添加劑，包含 0.1~10 重量份之主光澤劑，其具含氮芳香雜環或聚季銨鹽陽離子結構", "前案 D1 對應技術": "常規吡啶衍生物單一有機光澤劑", "前案 D2 對應技術": "硫脲類晶粒抑制劑", "符合性判定": "NO (不符/差異點)", "差異/進步性說明": "本案採用特定聚季銨鹽結構，在高電流密度區具備更強的陰極吸附極化能力，不易高溫裂解。"},
                {"要件編號": "Element 1B", "本案 Claim 1 技術要件": "包含 0.05~5 重量份之輔助細化劑，選自含硫或磺酸基有機抑制劑（如 MPS/SPS/MBI 類）", "前案 D1 對應技術": "游離磺酸鹽載體", "前案 D2 對應技術": "含硫醇基之界面整平劑", "符合性判定": "YES (字面讀取)", "差異/進步性說明": "公知之含硫去極化或微晶細化構件，用於輔助抑制樹枝狀結晶生成。"},
                {"要件編號": "Element 1C", "本案 Claim 1 技術要件": "該主光澤劑與輔助細化劑之重量比限定為 1:1 至 10:1，具特定吸附平衡比例", "前案 D1 對應技術": "未限定特定重量配比，由操作者隨機添加", "前案 D2 對應技術": "比例為 1:20 之微量添加系統", "符合性判定": "NO (不符/差異點)", "差異/進步性說明": "核心進步性特徵：特定 1:1~10:1 配比產生陰極極化過電位負移 50~200 mV 的協同效應，晶粒細化至 80 nm 以下且無脆化。"},
                {"要件編號": "Element 1D", "本案 Claim 1 技術要件": "包含 0.5~8 重量份之極化調節界面活性劑與溶劑載體，使鍍液於 0.5~5 A/dm² 寬電流密度下維持鏡面光澤", "前案 D1 對應技術": "非離子界面活性劑（PEG-400）", "前案 D2 對應技術": "陰離子界面活性劑", "符合性判定": "均等成立 (DOE)", "差異/進步性說明": "提供鍍浴基本潤濕與排氫消泡功效，屬通常知識者可等效置換之均等構件。"}
            ]
            st.session_state["last_oa_result"] = OA_ELECTROPLATING_DOC

        elif sel == "多光譜溫室作物病害早期偵測系統":
            st.session_state["patent_title_input"] = "多光譜溫室作物病害早期偵測系統"
            st.session_state["ipc_input_val"] = "A01G 9/24, G01N 21/84, G06V 20/10, G06T 7/00"
            st.session_state["cpc_input_val"] = "A01G 9/24, G01N 2021/8466, G06V 20/188"
            st.session_state["p1_n_val"] = "Target: 溫室作物與植物病害"
            st.session_state["p1_e_val"] = "greenhouse crop, plant disease, foliage pathogen, tomato crop"
            st.session_state["p1_z_val"] = "溫室作物, 植物病害, 葉片病原, 作物健康, 番茄病害"
            st.session_state["p2_n_val"] = "Mechanism: 多光譜感測與邊緣影像推論"
            st.session_state["p2_e_val"] = "multispectral imaging, hyperspectral sensor, narrowband reflectance, edge computing"
            st.session_state["p2_z_val"] = "多光譜影像, 高光譜感測, 窄波段反射率, 邊緣運算, 深度學習推論"
            st.session_state["p3_n_val"] = "Effect: 潛伏早期偵測與即時警報"
            st.session_state["p3_e_val"] = "early lesion detection, asymptomatic stage, pre-symptomatic diagnosis, real-time alert"
            st.session_state["p3_z_val"] = "早期病斑偵測, 潛伏期診斷, 症狀前檢測, 即時告警, 降低誤判"
            st.session_state["claims_data"] = [
                {"要件編號": "Element 1A", "本案 Claim 1 技術要件": "一多光譜感測模組，配置於移動軌道，具有特定吸收峰窄波段濾波感測器", "前案 D1 對應技術": "常規 RGB 廣角監視器", "前案 D2 對應技術": "手持式分光輻射計", "符合性判定": "NO (不符/差異點)", "差異/進步性說明": "本案特定窄波段針對植物水分及葉綠素吸收峰，非可見光全光譜影像"},
                {"要件編號": "Element 1B", "本案 Claim 1 技術要件": "一邊緣推論處理器，對多光譜影像執行植被指數（NDVI/PRI）正規化降維校正", "前案 D1 對應技術": "影像壓縮後直接回傳伺服器", "前案 D2 對應技術": "離線電腦以 MATLAB 批次運算", "符合性判定": "NO (不符/差異點)", "差異/進步性說明": "本案於感測端完成即時反光補償與植被指數特徵化，降低傳輸頻寬"},
                {"要件編號": "Element 1C", "本案 Claim 1 技術要件": "一病斑早期預警神經網路模型，根據特徵化多光譜資訊預測前症狀潛伏病灶", "前案 D1 對應技術": "色差比對判定枯黃斑塊", "前案 D2 對應技術": "葉片病徵分類 CNN", "符合性判定": "NO (不符/差異點)", "差異/進步性說明": "本案能在葉片肉眼尚未顯性變色前 48 小時識別隱性病原感染"},
                {"要件編號": "Element 1D", "本案 Claim 1 技術要件": "一環控連動介面，當接收預警訊號時觸發特定分區通風調節與精準噴灑", "前案 D1 對應技術": "警報訊息推播至使用者手機", "前案 D2 對應技術": "全區定時自動噴灌", "符合性判定": "YES (字面讀取)", "差異/進步性說明": "結合定位分區執行隔離防護之閉迴路控制"}
            ]

        elif sel == "邊緣運算光學瑕疵檢測":
            st.session_state["patent_title_input"] = "基於邊緣運算之即時影像瑕疵檢測系統"
            st.session_state["ipc_input_val"] = "G06T 7/00, G01N 21/88"
            st.session_state["cpc_input_val"] = "G06V 10/00"
            st.session_state["p1_n_val"] = "Target: 瑕疵檢測"
            st.session_state["p1_e_val"] = "defect detection, flaw inspection, surface anomaly"
            st.session_state["p1_z_val"] = "瑕疵檢測, 缺陷檢驗, 表面異常"
            st.session_state["p2_n_val"] = "Mechanism: 邊緣運算與視覺推論"
            st.session_state["p2_e_val"] = "edge computing, neural network, real-time inferenc*"
            st.session_state["p2_z_val"] = "邊緣運算, 神經網絡, 即時推論, 深度學習"
            st.session_state["p3_n_val"] = "Effect: 低延遲與高精度"
            st.session_state["p3_e_val"] = "low latency, high throughput, false positive reduction"
            st.session_state["p3_z_val"] = "低延遲, 降低誤判, 即時處理"
            st.session_state["claims_data"] = [
                {"要件編號": "Element 1A", "本案 Claim 1 技術要件": "一工業高速相機，擷取產線物件表面光學影像", "前案 D1 對應技術": "CCD 線型感測器", "前案 D2 對應技術": "面陣相機", "符合性判定": "YES (字面讀取)", "差異/進步性說明": "公知取像構件"},
                {"要件編號": "Element 1B", "本案 Claim 1 技術要件": "一邊緣推論加速模組，具備特定神經網路剪枝架構", "前案 D1 對應技術": "工控機 GPU 集中運算", "前案 D2 對應技術": "雲端伺服器推論", "符合性判定": "NO (不符/差異點)", "差異/進步性說明": "邊緣端低功耗輕量化推論"},
                {"要件編號": "Element 1C", "本案 Claim 1 技術要件": "一動態閾值缺陷分割演算法，抑制表面反光雜訊", "前案 D1 對應技術": "固定灰階值二值化", "前案 D2 對應技術": "局部自適應閥值", "符合性判定": "均等成立 (DOE)", "差異/進步性說明": "進一步考量動態曝光補償"}
            ]

    st.sidebar.selectbox(
        "選擇技術模板快速填入：",
        ["自訂輸入", "貴金屬電鍍晶粒細化光澤劑", "多光譜溫室作物病害早期偵測系統", "邊緣運算光學瑕疵檢測"],
        key="template_select_key",
        on_change=apply_template
    )

    st.subheader("1. 發明標的名稱與 AI 自動拆解")
    col_input1, col_input2 = st.columns([3, 1])

    with col_input1:
        target_title = st.text_input(
            "請輸入專利標的名稱：",
            key="patent_title_input",
            placeholder="例如：晶圓搬運機械手臂動態抑振控制系統 或 用於貴金屬電鍍之晶粒細化光澤添加劑組成物"
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
            with st.spinner("🤖 正在調用 Gemini 拆解技術特徵（含急速輪替保護）..."):
                try:
                    ai_res = analyze_patent_with_gemini(user_api_key.strip(), target_title.strip())

                    st.session_state["ipc_input_val"] = ai_res.get("ipc", "")
                    st.session_state["cpc_input_val"] = ai_res.get("cpc", "")
                    st.session_state["p1_n_val"] = ai_res.get("pillar_a_name", "Target: 應用標的")
                    st.session_state["p1_e_val"] = ai_res.get("pillar_a_en", "")
                    st.session_state["p1_z_val"] = ai_res.get("pillar_a_zh", "")
                    st.session_state["p2_n_val"] = ai_res.get("pillar_b_name", "Mechanism: 核心手段")
                    st.session_state["p2_e_val"] = ai_res.get("pillar_b_en", "")
                    st.session_state["p2_z_val"] = ai_res.get("pillar_b_zh", "")
                    st.session_state["p3_n_val"] = ai_res.get("pillar_c_name", "Effect: 技術功效")
                    st.session_state["p3_e_val"] = ai_res.get("pillar_c_en", "")
                    st.session_state["p3_z_val"] = ai_res.get("pillar_c_zh", "")
                    
                    if ai_res.get("claim_elements"):
                        st.session_state["claims_data"] = ai_res.get("claim_elements")

                    st.success("🎉 Gemini AI 拆解完成！三支柱欄位與 Claims 已 100% 同步填入！")
                    st.rerun()
                except Exception as e:
                    st.error(f"AI 呼叫失敗，請稍後重試。詳細原因: {e}")

    col_class1, col_class2 = st.columns(2)
    with col_class1:
        ipc_input = st.text_input("IPC 分類號 (逗號隔開)", key="ipc_input_val", placeholder="例: C25D 3/46, C25D 3/48")
    with col_class2:
        cpc_input = st.text_input("CPC 分類號 (逗號隔開)", key="cpc_input_val", placeholder="例: C25D 3/46, C25D 3/64")

    st.markdown("---")
    st.subheader("2. 技術三支柱特徵拆解")

    col_p1, col_p2, col_p3 = st.columns(3)
    with col_p1:
        st.markdown("#### 支柱 A：應用標的 (Target)")
        p1_name = st.text_input("支柱 A 名稱", key="p1_n_val")
        p1_en = st.text_area("英文關鍵字 (逗號隔開)", key="p1_e_val", height=100)
        p1_zh = st.text_area("中文關鍵字 (逗號隔開)", key="p1_z_val", height=100)

    with col_p2:
        st.markdown("#### 支柱 B：核心手段 (Mechanism)")
        p2_name = st.text_input("支柱 B 名產", key="p2_n_val")
        p2_en = st.text_area("英文關鍵字 (逗號隔開)", key="p2_e_val", height=100)
        p2_zh = st.text_area("中文關鍵字 (逗號隔開)", key="p2_z_val", height=100)

    with col_p3:
        st.markdown("#### 支柱 C：技術功效 (Effect)")
        p3_name = st.text_input("支柱 C 名稱", key="p3_n_val")
        p3_en = st.text_area("英文關鍵字 (逗號隔開)", key="p3_e_val", height=100)
        p3_zh = st.text_area("中文關鍵字 (逗號隔開)", key="p3_z_val", height=100)

    st.markdown("---")

    # ==========================================================================
    # 3. 引證前案自動爬取與比對
    # ==========================================================================
    st.subheader("3. 引證前案專利號爬取與自動比對 (Auto-fetch Prior Art)")
    st.caption("輸入引證案公開/公告號（支援 US、EP、WO、CN、TW 等），自動自 Google Patents 爬取內容，並由 AI 比對填入下表指定的前案欄位。")

    col_fetch1, col_fetch2, col_fetch3 = st.columns([2, 1, 1])
    with col_fetch1:
        target_pno = st.text_input("前案專利號 (公開號/公告號)：", placeholder="例如：US11578418B2、CN110016700A 或 US8608931B2", key="fetch_pno_input")
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
            with st.spinner(f"🌐 正在爬取 {target_pno.strip()} 並啟動多模型急速輪替比對..."):
                try:
                    p_data = fetch_patent_data_from_google(target_pno.strip())
                    st.session_state["last_fetched_patent"] = p_data

                    curr_claims = st.session_state["claims_data"]
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

                    st.session_state["claims_data"] = curr_claims
                    st.success(f"✅ 成功擷取專利：【{p_data['patent_no']}】{p_data['title']}，已完成對應比對！")
                    st.rerun()

                except Exception as e:
                    st.error(f"爬取或比對失敗: {e}")

    if st.session_state["last_fetched_patent"]:
        last_p = st.session_state["last_fetched_patent"]
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
    current_claims = st.session_state["claims_data"]
    
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
        key="claim_editor_live"
    )

    col_claim_oa1, col_claim_oa2 = st.columns([2, 1])
    with col_claim_oa1:
        st.caption("💡 提示：若表格中有被判定為「NO (不符/差異點)」的元件，可直接利用右方按鈕一鍵撰寫《專利法》第22條第2項進步性答辯理由。")
    with col_claim_oa2:
        quick_oa_btn = st.button("⚖️ 一鍵生成《專利法》第22條進步性申復理由", use_container_width=True)

    if quick_oa_btn:
        if not user_api_key.strip():
            st.error("請先於左側側邊欄輸入有效的 Gemini API Key！")
        else:
            with st.spinner("🤖 正在自比對矩陣提煉差異點，撰寫專利法第22條進步性申復理由書..."):
                diff_rows = []
                for _, r in edited_df.iterrows():
                    elem = r.get("要件編號", "")
                    claim_desc = r.get("本案 Claim 1 技術要件", "")
                    d1_desc = r.get("前案 D1 對應技術", "")
                    diff_note = r.get("差異/進步性說明", "")
                    diff_rows.append(f"【{elem}】本案要件：{claim_desc}；前案技術：{d1_desc}；差異與進步功效：{diff_note}")

                diff_summary = "\n".join(diff_rows)
                rejection_summary = "審查官認為本案 Claim 1 技術要件已被引證前案揭露或為所屬技術領域具通常知識者所能輕易組合完成，認定欠缺進步性。"
                
                try:
                    quick_oa_res = generate_oa_response_with_gemini(
                        user_api_key.strip(),
                        "專利法第 22 條第 2 項（進步性核駁）",
                        target_title if target_title else "本發明專利申請案",
                        rejection_summary,
                        diff_summary
                    )
                    st.session_state["last_oa_result"] = quick_oa_res
                    st.success("🎉 進步性申復理由書產生完成！請至下方預覽或切換至法規分頁查看。")
                except Exception as e:
                    st.error(f"生成失敗: {e}")

    if st.session_state.get("last_oa_result"):
        with st.expander("📄 檢視最新產出之專利申復答辯理由書", expanded=True):
            oa_display_text = st.session_state["last_oa_result"]
            st.text_area("申復理由書全文：", value=oa_display_text, height=350, key="quick_oa_preview_box")
            col_oa_copy, col_oa_dl = st.columns(2)
            with col_oa_copy:
                render_copy_button(oa_display_text, "📋 快速複製申復理由全文", button_id="copyQuickOA")
            with col_oa_dl:
                current_timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                st.download_button(
                    label="📥 下載申復理由書檔案 (.txt)",
                    data=oa_display_text.encode("utf-8"),
                    file_name=f"Patent_OA_Response_{current_timestamp}.txt",
                    mime="text/plain;charset=utf-8",
                    type="primary",
                    use_container_width=True
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
            st.markdown("#### 🌐 Google Patents / Espacenet 檢索式 (官方相容規範)")
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
            st.caption("已包含：三支柱、官方標準檢索式、Claims 檢核表、比對矩陣，以及【附錄：引證前案原文摘錄】。")

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
# TAB 2: 商標權模組
# ==============================================================================
with tab_trademark:
    st.subheader("🏷️ 商標尼斯分類佈局與 TIPO 規範圖樣產生器")
    st.markdown("評估商標識別性（Distinctiveness）、自動推薦第 09/42 類商品，並支援上傳 Logo 圖片、多行自由換行、對齊排版與行距微調合成符合智財局規範之申請圖檔。")

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
                with st.spinner("🤖 正在調用 Gemini 評估商標識別性與分類（含急速輪替保護）..."):
                    try:
                        st.session_state["tm_analysis"] = analyze_trademark_with_gemini(user_api_key.strip(), tm_brand.strip(), tm_desc.strip())
                        st.success("🎉 商標分析完成！")
                    except Exception as e:
                        st.error(f"分析失敗: {e}")

        if st.session_state.get("tm_analysis"):
            res = st.session_state["tm_analysis"]
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
        st.markdown("#### 2. TIPO 電子送件商標圖樣即時產生器 (含 Logo 合成與彈性換行)")
        st.caption("官方硬性規範：8×8 公分、300 DPI、945×945 px、純白底色、RGB 模式 JPEG。")

        tm_multiline_text = st.text_area(
            "圖樣文字內容（支援按下 Enter 自由換行）：",
            value=tm_brand.strip() if tm_brand.strip() else "葉語\nSpectrIQ",
            height=75,
            help="可直接按 Enter 鍵進行多行換行排列，例如第一行英文、第二行中文。"
        )

        uploaded_logo = st.file_uploader("選填：上傳品牌 Logo 圖檔 (支援 PNG、JPG，透明底自動填白)", type=["png", "jpg", "jpeg"])

        col_ctrl1, col_ctrl2 = st.columns(2)
        with col_ctrl1:
            layout_options = ["純文字模式"]
            if uploaded_logo is not None:
                layout_options = ["複合商標：上圖下文", "複合商標：左圖右文"] + layout_options
            layout_choice = st.selectbox("圖樣排版方式：", layout_options)

        with col_ctrl2:
            align_choice = st.selectbox("文字對齊方式：", ["置中對齊", "靠左對齊", "靠右對齊"])

        col_slider1, col_slider2 = st.columns(2)
        with col_slider1:
            font_size_val = st.slider("文字字級大小 (Font Size)：", min_value=28, max_value=120, value=58 if uploaded_logo else 68, step=2)
        with col_slider2:
            spacing_ratio_val = st.slider("行距倍率 (Line Spacing)：", min_value=0.1, max_value=1.5, value=0.35, step=0.05, help="調整多行文字之間的上下間距比例。")

        if tm_multiline_text.strip():
            img_bytes = create_tipo_trademark_bytes(
                text=tm_multiline_text.strip(),
                layout=layout_choice,
                font_size=font_size_val,
                text_align=align_choice,
                line_spacing_ratio=spacing_ratio_val,
                logo_file=uploaded_logo
            )
            st.image(img_bytes, caption="📸 圖樣預覽 (8x8 cm @ 300 DPI 標準白底)", width=320)

            clean_first_line = re.sub(r'[\r\n\s]+', '_', tm_multiline_text.strip()[:20])
            clean_filename = f"trademark_{clean_first_line}.jpg"
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
            st.warning("請先輸入商標文字以生成圖樣。")

# ==============================================================================
# TAB 3: 智財法規速查 (專利法、商標法、營業秘密法與化學配方專題)
# ==============================================================================
with tab_laws:
    st.subheader("⚖️ 智財法規速查指南 (含化學配方專利 ＆ 營業秘密法)")
    st.markdown("快速檢索與參考台灣**《專利法》**、**《商標法》**、**《營業秘密法》**核心條文，以及**化學配方發明審查基準**，並可由 **AI 一鍵模擬撰寫審查意見申復答辯書**。")

    col_filter1, col_filter2 = st.columns([1, 2])
    with col_filter1:
        law_type_filter = st.selectbox(
            "篩選法規類別：",
            ["全部法規", "專利法", "化學配方專利專題", "營業秘密法", "商標法"]
        )
    with col_filter2:
        search_kw = st.text_input("輸入條文、標題或關鍵字快速過濾：", placeholder="例如：新穎性、協同效應、秘密性、合理保密措施、境外使用罪")

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
        badge_map = {
            "專利法": "📄 專利法",
            "化學配方專利專題": "🧪 化學配方專題",
            "營業秘密法": "🔒 營業秘密法",
            "商標法": "🏷️ 商標法"
        }
        badge = badge_map.get(item["category"], "⚖️ 智財法規")
        expander_title = f"{badge} ｜ {item['article']}：{item['title']}"
        with st.expander(expander_title, expanded=True if search_kw.strip() else False):
            st.markdown(f"**🔍 關鍵字標籤**：`{item['keywords']}`")
            st.markdown("##### 📜 法定條文內容：")
            st.code(item["text"], language="text")
            st.markdown("##### 💡 審查實務、企業管理與答辯要點：")
            st.info(item["explanation"])

    # --------------------------------------------------------------------------
    # AI 申復答辯理由書撰寫模組 (含化學配方協同效應專用範本)
    # --------------------------------------------------------------------------
    st.markdown("---")
    st.subheader("🤖 AI 智財局審查意見申復理由書產生器 (OA Response Generator)")
    st.caption("遭遇智慧財產局審查意見通知函（Office Action）核駁或異議爭議時，可依據引證案事實與抗辯要點，一鍵生成代理人規格之申復答辯理由書。")

    oa_template_options = [
        "化學配方專利第 22 條第 2 項（配比協同功效 / 突變性增益抗辯）",
        "化學配方專利第 26 條第 1 項（說明書可據以實現 / 充分揭露抗辯）",
        "商標法第 30 條第 1 項第 10 款（商品非類似/不致混淆抗辯 - 例: 音箱 vs. 展示架）",
        "專利法第 22 條第 2 項（進步性核駁 / 容易思及完成）",
        "專利法第 22 條第 1 項（新穎性核駁 / 單一前案已揭露）",
        "商標法第 29 條第 1 項（缺乏先天識別性 / 說明性用語抗辯）",
        "商標法第 30 條第 1 項第 11 款（著名商標淡化 / 減損信譽抗辯）"
    ]
    oa_law = st.selectbox("選擇審查意見/爭議所適用的法定條款範本：", oa_template_options)

    if "化學配方專利第 22 條" in oa_law:
        default_oa_target = st.session_state.get("patent_title_input", "用於貴金屬電鍍之晶粒細化光澤添加劑組成物")
        default_oa_grounds = (
            "審查官認為本案 Claim 1 所界定之化學成分種類均為已知常規有機化合物，"
            "且各成分於先前技術中已被揭露，認定將該等成分組合並限定特定配比範圍，"
            "僅為所屬技術領域中具有通常知識者依先前技術能輕易置換完成，不具進步性。"
        )
        default_oa_diffs = (
            "1. 引證案皆未揭露本案所限定之特定臨界重量配比（例如 1:1 至 10:1）。\n"
            "2. 本案成分在此特定配比範圍內，產生了『無法預期之技術功效（Unexpected Results）』，"
            "誘發陰極極化過電位顯著負移 50~200 mV，將晶粒強制細化至 80 nm 以下且無脆化，具備突變性協同增效（Synergistic Effect）。\n"
            "3. 說明書實施例與比較例數據明確證明，偏離此配比區間功效立即大幅衰退，具有顯著臨界技術貢獻。"
        )
    elif "化學配方專利第 26 條" in oa_law:
        default_oa_target = st.session_state.get("patent_title_input", "功能性化學組成物")
        default_oa_grounds = "審查官認為說明書未充分揭露特定反應參數或原料來源，使同業無法據以實現該配方之預期功效。"
        default_oa_diffs = (
            "1. 說明書已詳盡載明至少 3 組完整實施例之加料順序、反應溫度與剪切速率。\n"
            "2. 本案所使用之主要基質與添加劑皆為市場可購得之市售化學品，所屬技術領域具通常知識者無需過度過度實驗即能再現。\n"
            "3. 申請專利範圍所界定之數值範圍，均有具體實施例與比較例數據支持，符合第 26 條之充分揭露標準。"
        )
    elif "商品非類似/不致混淆抗辯" in oa_law:
        default_oa_target = "本案「Lakis」音箱展示架 vs. 美商「Lakis」音箱 (揚聲器)"
        default_oa_grounds = (
            "相對人（美商 Lakis 音響公司）指控答辯人於國內生產銷售音箱專用展示架命名為「Lakis」，"
            "商標文字完全相同，且展示架與音箱具周邊配套關係，認為構成《商標法》第 30 條第 1 項第 10 款及第 68 條之商品類似且有致混淆誤認之虞。"
        )
        default_oa_diffs = (
            "1. 商品性質與功能用途截然有別：音箱（第09類）為精密電聲轉換播放電氣器材；展示架（第20/06類）為物理性支撐承重及陳列減震之五金家具結構件，功能與材料科學全然不同。\n"
            "2. 產製主體領域分流，無跨界常態：音響原廠通常不兼營家具板金製造，消費者對音響本體與陳列腳架產製主體分離具有充分市場通念。\n"
            "3. 專業購買者注意程度極高：選購音箱展示架多為音響愛好者或專業工程人員，對機械規格、尺寸、承重與產地極為敏銳，施以較高注意，不致混淆來源。\n"
            "4. 相對人商標未達著名程度：相對人在台並無大量宣傳與市佔實績，不得任意擴大排他範圍跨類阻礙合理周邊商業自由競爭。"
        )
    elif "進步性核駁" in oa_law:
        default_oa_target = st.session_state.get("patent_title_input", "專利技術標的")
        default_oa_grounds = "審查官認為本案 Claim 1 所請技術特徵，為所屬技術領域具通常知識者結合引證案 D1 與引證案 D2 所能輕易置換完成，不具進步性。"
        default_oa_diffs = "引證案 D1 與 D2 存在反向教示（Teaching Away），且本案結合產生了先前技術所無法達成之閉迴路控制與協同功效。"
    elif "新穎性核駁" in oa_law:
        default_oa_target = st.session_state.get("patent_title_input", "專利技術標的")
        default_oa_grounds = "審查官認為本案 Claim 1 技術特徵已被引證案 D1 完全揭露，欠缺新穎性。"
        default_oa_diffs = "引證案 D1 所揭露之構件在物理連結、特定排列組合與實質動作邏輯上，與本案 Claim 1 明確記載之關鍵限制條件不同，依全要件原則並未被其完全讀取。"
    elif "缺乏先天識別性" in oa_law:
        default_oa_target = "擬申請商標名稱"
        default_oa_grounds = "審查官認為本件商標文字直接說明所指定商品/服務之品質、功能或用途，缺乏先天識別性。"
        default_oa_diffs = "本件商標文字具獨創隱喻或暗示意境（Suggestive），非產品功能之直接描述；且申請人經長期投入商業宣傳，在交易上已足以表彰商品來源並取得後天識別性。"
    else:
        default_oa_target = "商標標的名稱"
        default_oa_grounds = "審查官或異議人主張商標有致消費者混淆或減損著名商標信譽之虞。"
        default_oa_diffs = "兩造商標於外觀、觀念及讀音具顯著區隔，且指定商品市場通路、消費客群互殊，無致混淆誤認或淡化信譽之虞。"

    col_oa1, col_oa2 = st.columns(2)
    with col_oa1:
        oa_target = st.text_input("本案專利標的 / 商標名稱（可自訂）：", value=default_oa_target)
        oa_grounds = st.text_area("審查意見通知函（核駁/異議理由）主要指控：", value=default_oa_grounds, height=130)

    with col_oa2:
        oa_diffs = st.text_area("申請人/答辯人主張之實體論據（技術功效 / 跨類不致混淆事實）：", value=default_oa_diffs, height=195)

    st.write("")
    gen_oa_btn = st.button("✨ 產生申復答辯理由書草稿", type="primary", use_container_width=True)

    if gen_oa_btn:
        if not user_api_key.strip():
            st.error("請先於左側側邊欄輸入有效的 Gemini API Key！")
        elif not oa_target.strip():
            st.warning("請填寫標的名稱。")
        else:
            with st.spinner("🤖 正在調用 Gemini（智財代理人引擎）撰寫專業申復理由書..."):
                try:
                    oa_result = generate_oa_response_with_gemini(
                        user_api_key.strip(),
                        oa_law,
                        oa_target.strip(),
                        oa_grounds.strip(),
                        oa_diffs.strip()
                    )
                    st.session_state["last_oa_result"] = oa_result
                    st.success("🎉 申復答辯理由書草稿產生完成！")
                except Exception as e:
                    st.error(f"生成失敗: {e}")

    if st.session_state.get("last_oa_result"):
        oa_doc = st.session_state["last_oa_result"]
        st.markdown("#### 📄 申復答辯理由書草稿預覽")
        st.text_area("申復理由書全文內容（可線上直接微調）：", value=oa_doc, height=350, key="oa_general_textarea")

        col_oa_copy, col_oa_dl = st.columns(2)
        with col_oa_copy:
            render_copy_button(oa_doc, "📋 一鍵複製申復書全文", button_id="copyOAResponse")
        with col_oa_dl:
            oa_time = datetime.now().strftime('%Y%m%d_%H%M%S')
            st.download_button(
                "📥 下載申復理由書 (.txt)",
                data=oa_doc.encode("utf-8"),
                file_name=f"OA_Response_{oa_time}.txt",
                mime="text/plain;charset=utf-8",
                type="secondary",
                use_container_width=True
            )

    st.markdown("---")
    st.markdown("#### 🌐 官方全國法規資料庫即時連結")
    col_ext1, col_ext2, col_ext3, col_ext4 = st.columns(4)
    with col_ext1:
        st.link_button("📜 中華民國《專利法》完整法條", "https://law.moj.gov.tw/LawClass/LawAll.aspx?pcode=J0070007", use_container_width=True)
    with col_ext2:
        st.link_button("🔒 中華民國《營業秘密法》完整法條", "https://law.moj.gov.tw/LawClass/LawAll.aspx?pcode=J0070028", use_container_width=True)
    with col_ext3:
        st.link_button("🏷️ 中華民國《商標法》完整法條", "https://law.moj.gov.tw/LawClass/LawAll.aspx?pcode=J0070001", use_container_width=True)
    with col_ext4:
        st.link_button("🏛️ 智慧財產局專利/商標審查基準", "https://www.tipo.gov.tw/", use_container_width=True)
