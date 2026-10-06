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
    name: str
    en_keywords: List[str] = field(default_factory=list)
    zh_keywords: List[str] = field(default_factory=list)

class PatentSearchBuilder:
    PLACEHOLDER_BLACKLIST = {
        "target system", "mechanism", "effect", "none", "null", "undefined",
        "n/a", "na", "g06f 17/00", "待確認", "無", "未指定"
    }

    def __init__(self, target_title: str):
        self.target_title = self._clean_text(target_title)
        self.ipc_classes: List[str] = []
        self.cpc_classes: List[str] = []
        self.pillars: List[TechnicalPillar] = []

    @staticmethod
    def _clean_text(text: str) -> str:
        if not text:
            return ""
        cleaned = re.sub(r'[;\r\n]+', ' ', str(text))
        cleaned = re.sub(r'\s+', ' ', cleaned).strip()
        return cleaned

    @classmethod
    def _clean_keyword(cls, kw: str) -> str:
        cleaned = cls._clean_text(kw).strip('",\'')
        if not cleaned or cleaned.lower() in cls.PLACEHOLDER_BLACKLIST:
            return ""
        return cleaned

    @classmethod
    def _normalize_class_code(cls, code: str) -> str:
        if not code:
            return ""
        cleaned = re.sub(r'[;,\s]+', '', str(code)).upper()
        cleaned = re.sub(r'^(CPC=|IPC=|IC=)', '', cleaned)
        return cleaned

    def add_ipc(self, *ipc_codes: str) -> "PatentSearchBuilder":
        for code in ipc_codes:
            for item in re.split(r'[,;]+', str(code)):
                norm = self._normalize_class_code(item)
                if norm and norm.lower() not in self.PLACEHOLDER_BLACKLIST:
                    self.ipc_classes.append(norm)
        return self

    def add_cpc(self, *cpc_codes: str) -> "PatentSearchBuilder":
        for code in cpc_codes:
            for item in re.split(r'[,;]+', str(code)):
                norm = self._normalize_class_code(item)
                if norm and norm.lower() not in self.PLACEHOLDER_BLACKLIST:
                    self.cpc_classes.append(norm)
        return self

    def add_pillar(self, name: str, en_keywords: List[str], zh_keywords: List[str]) -> "PatentSearchBuilder":
        cleaned_en = [self._clean_keyword(kw) for kw in en_keywords]
        cleaned_zh = [self._clean_keyword(kw) for kw in zh_keywords]

        self.pillars.append(
            TechnicalPillar(
                name=self._clean_text(name),
                en_keywords=[kw for kw in cleaned_en if kw],
                zh_keywords=[kw for kw in cleaned_zh if kw]
            )
        )
        return self

    def to_google_patents_query(self, include_effect_pillar: bool = False) -> str:
        pillar_blocks = []
        pillars_to_use = self.pillars if include_effect_pillar else self.pillars[:2]

        for p in pillars_to_use:
            if p.en_keywords:
                selected_kws = p.en_keywords[:4]
                formatted = [f'"{kw}"' if " " in kw else kw for kw in selected_kws]
                pillar_blocks.append(f"({' OR '.join(formatted)})")

        keyword_part = " AND ".join(pillar_blocks) if pillar_blocks else ""
        classes = self.cpc_classes or self.ipc_classes
        unique_classes = list(dict.fromkeys(classes))

        class_part = ""
        if unique_classes:
            formatted_classes = [f"CPC={c}" for c in unique_classes]
            class_part = f"({' OR '.join(formatted_classes)})"

        if keyword_part and class_part:
            result = f"{keyword_part} AND {class_part}"
        else:
            result = keyword_part or class_part

        return result.rstrip("; ").strip()

    def to_gpss_query(self, search_fields: str = "TI,AB,CL", include_effect_pillar: bool = True) -> str:
        pillar_blocks = []
        pillars_to_use = self.pillars if include_effect_pillar else self.pillars[:2]

        for p in pillars_to_use:
            all_kw = p.zh_keywords + p.en_keywords
            if all_kw:
                selected_kw = all_kw[:5]
                formatted = [f'"{kw}"' if " " in kw else kw for kw in selected_kw]
                pillar_blocks.append(f"({' OR '.join(formatted)})")

        query_body = " AND ".join(pillar_blocks) if pillar_blocks else ""
        formatted_query = f"{search_fields}=({query_body})" if query_body else ""

        unique_ipc = list(dict.fromkeys(self.ipc_classes))
        if unique_ipc:
            ipc_block = " OR ".join([f'"{code}"*' for code in unique_ipc])
            if formatted_query:
                formatted_query += f" AND IC=({ipc_block})"
            else:
                formatted_query = f"IC=({ipc_block})"

        return formatted_query.rstrip("; ").strip()

    def generate_report_text(self, claim_chart_df: pd.DataFrame = None, prior_art_data: dict = None) -> str:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        lines = [
            "=" * 85,
            f"智慧財產權整合檢索與技術特徵分析報告 (含 Claims 檢核、比對矩陣與前案附錄)",
            f"產出時間：{now}",
            "=" * 85,
            f"\n【一、發明標的與分類設定】",
            f"標的名稱：{self.target_title}",
            f"IPC 分類號：{', '.join(self.ipc_classes) if self.ipc_classes else '未指定'}",
            f"CPC 分類號：{', '.join(self.cpc_classes) if self.cpc_classes else '未指定'}",
            f"\n【二、技術特徵支柱展開 (配方組分 / 物理機制 / 技術功效)】",
        ]
        
        for idx, p in enumerate(self.pillars, 1):
            lines.append(f"  {idx}. {p.name}")
            lines.append(f"     - 英文關鍵字：{', '.join(p.en_keywords) if p.en_keywords else '無'}")
            lines.append(f"     - 中文關鍵字：{', '.join(p.zh_keywords) if p.zh_keywords else '無'}")

        lines.extend([
            f"\n【三、各平台布林檢索邏輯式】",
            f"▶ Google Patents / Espacenet 檢索語法：",
            f"{self.to_google_patents_query(include_effect_pillar=False)}\n",
            f"▶ 台灣智慧財產局 (GPSS / TWPAT) 檢索語法：",
            f"{self.to_gpss_query(include_effect_pillar=True)}",
            f"\n" + "=" * 85,
            f"【四、申請專利範圍（Claims）合規檢核表】",
            f"=" * 85,
            f"[ ] 1. 標的定性清楚：獨立項前言是否清楚載明法定標的類型？",
            f"[ ] 2. 開閉鎖過渡詞：是否善用「包含（comprising）」或「由...組成（consisting of）」？",
            f"[ ] 3. 組分與數值臨界性：配方比例是否界定明確且在說明書中有臨界功效數據佐證？",
            f"[ ] 4. 馬庫西（Markush）格式：選擇性群組是否為具相似結構之均等物？",
            f"[ ] 5. 獨立項最小特徵原則：獨立項是否只保留達成核心相乘增效之必要成分？",
            f"[ ] 6. 名詞前置依據（Antecedent Basis）：所有冠上「該（said/the）」之構件是否有首次定義？",
            f"\n" + "=" * 85,
            f"【五、全要件原則（All-Elements Rule）前案比對分析矩陣】",
            f"=" * 85,
        ])

        if claim_chart_df is not None and not claim_chart_df.empty:
            lines.append(claim_chart_df.to_string(index=False))
        else:
            lines.append("（尚未建立比對要件資料）")

        if prior_art_data:
            lines.extend([
                f"\n\n【六、引證前案原文摘錄（附錄 Appendix）】",
                "=" * 85,
                f"專利號碼：{prior_art_data.get('patent_no', '未知')}",
                f"專利名稱：{prior_art_data.get('title', '未知')}",
                f"線上來源：{prior_art_data.get('url', '未知')}",
                f"\n--- 說明書摘要 (Abstract) ---",
                f"{prior_art_data.get('abstract', '無摘要內容')}",
                f"\n--- 申請專利範圍原文 (Claims) ---",
                f"{prior_art_data.get('claims', '無 Claims 內容')}",
                "=" * 85
            ])

        return "\n".join(lines)

# ==============================================================================
# 二、 專利號爬取與內容解析
# ==============================================================================
def fetch_patent_data_from_google(patent_no: str) -> dict:
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
# 三、 Gemini AI 自動重試引擎 (使用 gemini-3.6-flash 與 gemini-3.5-flash-lite)
# ==============================================================================
CANDIDATE_MODELS = [
    "gemini-3.6-flash",          # 主力：指定使用之最新旗艦 Flash
    "gemini-3.5-flash-lite",     # 次選：官方推薦之極速輕量化版本
    "gemini-3.5-flash",          # 備援：標準端點
    "gemini-3-flash"             # 備援：通用 Flash 端點
]

def _extract_json_from_text(raw_text: str):
    match = re.search(r'```(?:json)?\s*([\s\S]*?)\s*```', raw_text)
    if match:
        return json.loads(match.group(1))
    curly_match = re.search(r'(\{[\s\S]*\}|\[[\s\S]*\])', raw_text)
    if curly_match:
        return json.loads(curly_match.group(1))
    return json.loads(raw_text)

def generate_with_fallback(client, prompt: str, as_json: bool = True) -> str:
    last_err = None
    for model_name in CANDIDATE_MODELS:
        for attempt in range(2):
            try:
                config_args = {}
                if as_json:
                    config_args["response_mime_type"] = "application/json"

                response = client.models.generate_content(
                    model=model_name,
                    contents=prompt,
                    config=types.GenerateContentConfig(**config_args)
                )

                if response and response.text:
                    return response.text
            except Exception as e:
                err_str = str(e)
                last_err = err_str

                # 若遇到 404 或型號下線，立即換下一個備援模型
                if any(k in err_str.lower() for k in ["404", "not_found", "no longer available", "not supported"]):
                    break

                # 若遇到 503 或 429，等待退避重試
                if any(code in err_str for code in ["503", "429", "UNAVAILABLE", "RESOURCE_EXHAUSTED", "high demand"]):
                    time.sleep(2.0 * (attempt + 1))
                    continue

                if as_json and ("response_mime_type" in err_str or "json" in err_str.lower()):
                    try:
                        fallback_resp = client.models.generate_content(
                            model=model_name,
                            contents=prompt + "\n\n【重要：請務必以純 JSON 格式回傳，不要附加額外說明文字】"
                        )
                        if fallback_resp and fallback_resp.text:
                            return fallback_resp.text
                    except Exception:
                        pass
                break

    raise Exception(f"所有備援模型皆無法呼叫，最後錯誤：{last_err}")

def analyze_patent_with_gemini(api_key: str, title: str, is_chemical: bool = False) -> dict:
    client = genai.Client(api_key=api_key)
    
    type_instruction = ""
    if is_chemical:
        type_instruction = """
        【本案為化學/配方/材料組成物發明】：
        - 支柱 A (Target)：本案目標材料/組成物名稱、應用標的。
        - 支柱 B (Components)：本案關鍵化學成分、核心單體、官能基、添加劑。
        - 支柱 C (Property)：本案主要物化性能、突變功效、相乘協同增益 (如低損耗、抗剝離)。
        - Claim 1 要件：請嚴格拆解出該發明專利 Claim 1 的具體要件（如：Element 1A 主反應物、Element 1B 改質劑/填料、Element 1C 特定重量百分比臨界配比），不得泛泛而談！
        """
    else:
        type_instruction = """
        【本案為機械/電子/系統/機構/方法發明】：
        - 支柱 A (Target)：本案標的裝置、機構或系統名稱。
        - 支柱 B (Mechanism)：本案特徵核心構件、控制單元、傳動/感測架構或處理步驟。
        - 支柱 C (Effect)：本案解決之問題與技術功效 (如減重抗震、提高取放精度、降延遲)。
        - Claim 1 要件：請嚴格將該發明的獨立請求項（Claim 1）拆解成 3~4 個具體技術構件或步驟（如：Element 1A 基座/架構、Element 1B 驅動/處理單元、Element 1C 特徵連動/反饋機構），要具體切合標的名稱！
        """

    prompt = f"""
    你是一名專業的專利代理人與資深專利審查專家。
    請嚴格針對下列【發明專利標的名稱】進行深入分析，絕對禁止使用無關的套版資料，必須完全切合該技術領域：

    發明標的名稱："{title}"
    {type_instruction}

    請輸出繁體中文與專業英文，並以純 JSON 格式回傳，欄位規範如下：
    {{
        "ipc": "最貼近本發明之 IPC 分類號 2~4 個，用逗號隔開",
        "cpc": "最貼近本發明之 CPC 分類號 2~4 個，用逗號隔開",
        "pillar_a_name": "Target: 專屬標的裝置或材料名稱",
        "pillar_a_en": "英文關鍵字4~6個，逗號隔開",
        "pillar_a_zh": "中文同義詞4~6個，逗號隔開",
        "pillar_b_name": "Components/Mechanism: 本技術之核心組分或關鍵構件",
        "pillar_b_en": "英文關鍵字4~6個，逗號隔開",
        "pillar_b_zh": "中文同義詞4~6個，逗號隔開",
        "pillar_c_name": "Property/Effect: 本技術所達成之物理特性或功效",
        "pillar_c_en": "英文關鍵字4~6個，逗號隔開",
        "pillar_c_zh": "中文同義詞4~6個，逗號隔開",
        "claim_elements": [
            {{
                "要件編號": "Element 1A",
                "本案 Claim 1 技術要件": "切合該標的的第一個主要構件或主成分界定",
                "前案 D1 對應技術": "",
                "前案 D2 對應技術": "",
                "符合性判定": "待確認",
                "差異/進步性說明": "本要件相較於常規技術的特徵差異或功能說明"
            }},
            {{
                "要件編號": "Element 1B",
                "本案 Claim 1 技術要件": "切合該標的的第二個特徵構件、製程手段或改質添加劑",
                "前案 D1 對應技術": "",
                "前案 D2 對應技術": "",
                "符合性判定": "待確認",
                "差異/進步性說明": "核心發明特徵說明"
            }},
            {{
                "要件編號": "Element 1C",
                "本案 Claim 1 技術要件": "切合該標的的特定連動關係、控制邏輯或成分配比",
                "前案 D1 對應技術": "",
                "前案 D2 對應技術": "",
                "符合性判定": "待確認",
                "差異/進步性說明": "突變功效或非顯而易見性說明"
            }}
        ]
    }}
    """
    res_text = generate_with_fallback(client, prompt, as_json=True)
    return _extract_json_from_text(res_text)

def map_prior_art_with_gemini(api_key: str, current_elements: list, prior_art_data: dict, target_col: str) -> list:
    client = genai.Client(api_key=api_key)
    prompt = f"""
    你是一名資深專利代理人，正在執行「全要件原則（All-Elements Rule）」專利比對。
    【本案 Claim 1 現有要件清單】：{json.dumps(current_elements, ensure_ascii=False, indent=2)}
    【爬取到的引證前案資訊】：
    專利號：{prior_art_data['patent_no']}
    發明名稱：{prior_art_data['title']}
    摘要：{prior_art_data['abstract']}
    專利範圍：{prior_art_data['claims'][:3000]}

    請針對本案上述每一個要件（Element），提取該前案中是否有相對應之技術構件或組分配比。
    請嚴格回傳一個 JSON 陣列，長度必須與本案要件清單完全相同，格式如下：
    [
        {{
            "matched_tech": "前案在此要件揭露的具體對應構件或成分（若未揭露請寫『未揭露』）",
            "judgment": "YES (字面讀取) / NO (不符/差異點) / 均等成立 (DOE)",
            "diff_note": "針對該要件之差異分析或進步性技術功效"
        }}
    ]
    """
    res_text = generate_with_fallback(client, prompt, as_json=True)
    return _extract_json_from_text(res_text)

def analyze_trademark_with_gemini(api_key: str, brand_name: str, product_desc: str) -> dict:
    client = genai.Client(api_key=api_key)
    prompt = f"""
    你是一名專業的商標代理人與智財法務專家。
    請分析以下商標名稱與其應用之產品/服務：
    商標名稱："{brand_name}"
    產品/技術描述："{product_desc}"

    請嚴格依照以下 JSON 結構回傳：
    {{
        "distinctiveness_level": "獨創性(Fanciful) / 任意性(Arbitrary) / 暗示性(Suggestive) / 說明性(Descriptive)",
        "legal_risk_analysis": "針對該名稱之核駁風險簡析",
        "nice_classes": [
            {{
                "class_num": "第 01 類 或 第 12 類等",
                "group_codes": "群組碼",
                "recommended_items": "建議商品項目"
            }}
        ],
        "clearance_search_keywords": "檢索建議字"
    }}
    """
    res_text = generate_with_fallback(client, prompt, as_json=True)
    return _extract_json_from_text(res_text)

def generate_oa_response_with_gemini(api_key: str, law_article: str, target_name: str, rejection_grounds: str, diff_facts: str) -> str:
    client = genai.Client(api_key=api_key)
    prompt = f"""
    你是一名台灣資深專利代理人與智財律師。
    請依據台灣《經濟部智慧財產局（TIPO）》審查基準，針對下列核駁審查意見，撰寫一份結構嚴謹、條理分明的【申復理由書（草稿）】：
    【引用法條】：{law_article}
    【發明名稱】：{target_name}
    【核駁理由】：{rejection_grounds}
    【差異論據】：{diff_facts}

    請包含：一、案由與前言 二、審查基準法理依據（避免事後諸葛） 三、爭點具體比對與實體答辯理由 四、結論與懇請事項。
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
# 五、 智財核心法規資料庫
# ==============================================================================
IP_LAWS_DB = [
    {
        "category": "營業秘密法",
        "article": "營業秘密法 第 2 條",
        "title": "營業秘密之法定三要件",
        "keywords": "營業秘密, 秘密性, 經濟價值, 合理保密措施",
        "text": "本法所稱營業秘密，指方法、技術、製程、配方、程式、設計或其他可用於生產、銷售或經營之資訊，而符合下列要件者：\n一、非一般涉及該類資訊之人所知者（秘密性）。\n二、因其秘密性而具有實際或潛在之經濟價值者（經濟價值性）。\n三、所有人已採取合理之保密措施者（合理保密措施）。",
        "explanation": "營業秘密必須「嚴格具備」三要件，特別是合理保密措施（門禁、NDA、加密）。"
    },
    {
        "category": "專利法",
        "article": "專利法 第 22 條",
        "title": "專利三要件（產業利用性、新穎性、進步性）",
        "keywords": "新穎性, 進步性, 產業利用性, 協同增效",
        "text": "可供產業上利用之發明，無下列情事之一，得依本法申請專利：\n一、申請前已見於刊物者。\n二、申請前已公開實施者。\n三、申請前已為公眾所知悉者。\n發明雖無前項各款所列情事，但為其所屬技術領域中具有通常知識者依申請前之先前技術所能輕易完成時，仍不得依本法申請專利。",
        "explanation": "技術特徵若產生超出預期之功效，即具備法定進步性。"
    }
]

USER_MANUAL_MARKDOWN = """# 📖 智慧財產權整合工作台 操作手冊
1. **標的拆解**：輸入名稱並點選「✨ Gemini AI 自動拆解」。
2. **前案比對**：輸入專利號並點擊「📥 爬取並自動填入」。
3. **表格編輯**：直接於表格內編輯要件與進步性說明。
"""

# ==============================================================================
# 六、 Streamlit 介面與 Session State 同步管理
# ==============================================================================
st.set_page_config(
    page_title="智慧財產權整合工作台 (專利 ＆ 商標 ＆ 營業秘密)",
    page_icon="🛡️",
    layout="wide"
)

if "table_reset_counter" not in st.session_state:
    st.session_state["table_reset_counter"] = 0

if "search_history" not in st.session_state:
    st.session_state["search_history"] = [
        "高韌性熱塑性碳纖維自行車車架成型技術",
        "半導體先進封裝用低介電高散熱環氧樹脂填料組成物",
        "用於貴金屬電鍍之晶粒細化光澤添加劑組成物",
        "多光譜溫室作物病害早期偵測系統"
    ]

bindings = {
    "target_title_key": "",
    "ipc_key": "",
    "cpc_key": "",
    "p1_n_key": "Target: 應用標的",
    "p1_e_key": "",
    "p1_z_key": "",
    "p2_n_key": "Components: 核心手段/組分",
    "p2_e_key": "",
    "p2_z_key": "",
    "p3_n_key": "Property: 技術功效/物化特性",
    "p3_e_key": "",
    "p3_z_key": "",
    "is_chem_key": False,
    "claims_data": [
        {"要件編號": "Element 1A", "本案 Claim 1 技術要件": "", "前案 D1 對應技術": "", "前案 D2 對應技術": "", "符合性判定": "待確認", "差異/進步性說明": ""}
    ],
    "last_fetched_patent": None,
    "tm_analysis": None,
    "last_oa_result": None
}

for k, v in bindings.items():
    if k not in st.session_state:
        st.session_state[k] = v

st.title("🛡️ 智慧財產權整合工作台 (專利 ＆ 商標 ＆ 營業秘密)")
st.markdown("全方位整合 **專利檢索分析 (含化學配方發明)**、**營業秘密決策與合規矩陣**、**Claims 全要件比對**、**TIPO 商標規範圖樣** 與 **AI 答辯書產生器**。")

# ------------------------------------------------------------------------------
# 側邊欄：API 設定、範本快速載入、清空與操作手冊
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
    help="可在 Google AI Studio 免費申請。"
)

st.sidebar.markdown("---")
st.sidebar.header("📁 快速載入技術範本")

selected_template = st.sidebar.selectbox(
    "選擇要載入的範本：",
    [
        "高韌性碳纖維自行車車架 (複材成型)",
        "半導體封裝低介電環氧樹脂 (化學配方)",
        "貴金屬電鍍晶粒細化光澤劑 (化學配方)"
    ]
)

col_tmpl1, col_tmpl2 = st.sidebar.columns(2)
with col_tmpl1:
    if st.button("📥 載入範本", use_container_width=True):
        st.session_state["table_reset_counter"] += 1
        if selected_template == "高韌性碳纖維自行車車架 (複材成型)":
            st.session_state["target_title_key"] = "高韌性熱塑性碳纖維自行車車架成型技術"
            st.session_state["is_chem_key"] = False
            st.session_state["ipc_key"] = "B62K 3/02, B29C 70/46, B29K 105/08"
            st.session_state["cpc_key"] = "B62K 3/02, B29C 70/46, B29K 2307/04"
            st.session_state["p1_n_key"] = "Target: 熱塑性碳纖維車架管件"
            st.session_state["p1_e_key"] = "thermoplastic carbon fiber frame, bicycle frame, tubular chassis"
            st.session_state["p1_z_key"] = "熱塑性碳纖維車架, 自行車車架, 管件車架, 複合材料自行車"
            st.session_state["p2_n_key"] = "Mechanism: 內充氣袋輔助預浸布連續模壓成型"
            st.session_state["p2_e_key"] = "bladder molding, continuous prepreg, polyetherketone, compression molding"
            st.session_state["p2_z_key"] = "氣囊成型, 連續碳纖維預浸布, 聚醚醚酮熱塑基質, 高壓熱壓成型"
            st.session_state["p3_n_key"] = "Effect: 高耐衝擊衝擊韌性與快速回收再生"
            st.session_state["p3_e_key"] = "high impact toughness, fatigue resistance, recyclability, void reduction"
            st.session_state["p3_z_key"] = "高衝擊韌性, 抗疲勞斷裂, 可回收熱塑成型, 消除層間氣孔"
            st.session_state["claims_data"] = [
                {"要件編號": "Element 1A", "本案 Claim 1 技術要件": "一種自行車車架主體，包含複數個一體成型之連續纖維強化熱塑性複合材料中空管件", "前案 D1 對應技術": "熱固性環氧樹脂碳纖維車架", "前案 D2 對應技術": "金屬鋁合金管件焊接", "符合性判定": "NO (不符/差異點)", "差異/進步性說明": "本案採用連續纖維熱塑基質，具備極高耐落石衝擊抗崩裂性能。"},
                {"要件編號": "Element 1B", "本案 Claim 1 技術要件": "各管件接合處具有預置之熱熔膠結嵌件，經感應熱熔對接形成無縫榫接節點", "前案 D1 對應技術": "傳統碳纖布手糊包覆膠黏", "前案 D2 對應技術": "螺栓固定鎖附", "符合性判定": "NO (不符/差異點)", "差異/進步性說明": "熱熔對接節點無膠水應力集中老化之缺陷。"},
                {"要件編號": "Element 1C", "本案 Claim 1 技術要件": "管壁層間孔隙率小於 0.8%，且吸能韌性較同重量熱固性碳纖構件高出 40% 以上", "前案 D1 對應技術": "孔隙率約 2~3%", "前案 D2 對應技術": "金屬構件無層間孔隙特性", "符合性判定": "NO (不符/差異點)", "差異/進步性說明": "顯著提升衝擊韌性與結構安全性。"}
            ]
        elif selected_template == "半導體封裝低介電環氧樹脂 (化學配方)":
            st.session_state["target_title_key"] = "半導體先進封裝用低介電高散熱環氧樹脂填料組成物"
            st.session_state["is_chem_key"] = True
            st.session_state["ipc_key"] = "C08L 63/00, C08K 3/36, C08G 59/20, H01L 23/29"
            st.session_state["cpc_key"] = "C08L 63/00, C08K 3/36, H01L 23/295"
            st.session_state["p1_n_key"] = "Target: 先進封裝低介電樹脂"
            st.session_state["p1_e_key"] = "epoxy molding compound, underfill resin, semiconductor packaging"
            st.session_state["p1_z_key"] = "環氧模塑料, 底部填膠, 半導體封裝, 低介電基質"
            st.session_state["p2_n_key"] = "Components: 雙環戊二烯樹脂與偶合修飾球矽"
            st.session_state["p2_e_key"] = "dicyclopentadiene epoxy, spherical silica, silane coupling agent"
            st.session_state["p2_z_key"] = "雙環戊二烯環氧, 球形二氧化矽, 矽烷偶合劑, 活性硬化劑"
            st.session_state["p3_n_key"] = "Property: 低損耗與高散熱低應力"
            st.session_state["p3_e_key"] = "low dielectric dissipation Df, low CTE, thermal conductivity"
            st.session_state["p3_z_key"] = "低介電損耗 Df, 低熱膨脹係數 CTE, 高導熱率, 抑制晶圓翹曲"
            st.session_state["claims_data"] = [
                {"要件編號": "Element 1A", "本案 Claim 1 技術要件": "包含 100 重量份之主樹脂基質，其中包含至少 40 wt% 之雙環戊二烯型環氧寡聚物", "前案 D1 對應技術": "常規雙酚 A 型環氧樹脂", "前案 D2 對應技術": "環脂族液態環氧樹脂", "符合性判定": "NO (不符/差異點)", "差異/進步性說明": "DCPD 剛性脂環骨架大幅降低偶極極化，降低 Df 至 0.003 以下。"},
                {"要件編號": "Element 1B", "本案 Claim 1 技術要件": "包含 250 至 400 重量份之表面改質球形二氧化矽奈米粉體", "前案 D1 對應技術": "未經修飾之角狀石英粉", "前案 D2 對應技術": "常規胺基矽烷修飾球矽", "符合性判定": "NO (不符/差異點)", "差異/進步性說明": "表面修飾抑制團聚，高填充下維持流動度。"}
            ]
        else:
            st.session_state["target_title_key"] = "用於貴金屬電鍍之晶粒細化光澤添加劑組成物"
            st.session_state["is_chem_key"] = True
            st.session_state["ipc_key"] = "C25D 3/46, C25D 3/48, C25D 3/62"
            st.session_state["cpc_key"] = "C25D 3/46, C25D 3/48"
            st.session_state["p1_n_key"] = "Target: 貴金屬電鍍浴與接觸件"
            st.session_state["p1_e_key"] = "electroplating bath, gold electroplating, silver plating"
            st.session_state["p1_z_key"] = "電鍍浴, 鍍金, 鍍銀, 接觸端子, 貴金屬沉積"
            st.session_state["p2_n_key"] = "Components: 雜環季銨鹽與含硫細化劑協同"
            st.session_state["p2_e_key"] = "grain refiner, brightener, quaternary ammonium"
            st.session_state["p2_z_key"] = "晶粒細化劑, 光澤劑, 聚季銨鹽, 硫丙基二硫化物"
            st.session_state["p3_n_key"] = "Property: 奈米微晶緻密與耐磨抗氧化"
            st.session_state["p3_e_key"] = "nanocrystalline, dendritic suppression, wear resistance"
            st.session_state["p3_z_key"] = "奈米晶粒, 抑制枝晶, 低接觸阻抗, 耐磨耗"
            st.session_state["claims_data"] = [
                {"要件編號": "Element 1A", "本案 Claim 1 技術要件": "包含 0.1~10 重量份之主光澤劑，具含氮芳香雜環或聚季銨鹽結構", "前案 D1 對應技術": "常規吡啶衍生物單一有機光澤劑", "前案 D2 對應技術": "硫脲類晶粒抑制劑", "符合性判定": "NO (不符/差異點)", "差異/進步性說明": "聚季銨鹽結構具強陰極極化能力。"}
            ]
        st.success(f"已成功載入範本：{selected_template}")
        st.rerun()

with col_tmpl2:
    if st.button("🧹 清空所有", use_container_width=True):
        st.session_state["table_reset_counter"] += 1
        st.session_state["target_title_key"] = ""
        st.session_state["is_chem_key"] = False
        st.session_state["ipc_key"] = ""
        st.session_state["cpc_key"] = ""
        st.session_state["p1_n_key"] = "Target: 應用標的"
        st.session_state["p1_e_key"] = ""
        st.session_state["p1_z_key"] = ""
        st.session_state["p2_n_key"] = "Components: 核心手段/組分"
        st.session_state["p2_e_key"] = ""
        st.session_state["p2_z_key"] = ""
        st.session_state["p3_n_key"] = "Property: 技術功效/物化特性"
        st.session_state["p3_e_key"] = ""
        st.session_state["p3_z_key"] = ""
        st.session_state["claims_data"] = [
            {"要件編號": "Element 1A", "本案 Claim 1 技術要件": "", "前案 D1 對應技術": "", "前案 D2 對應技術": "", "符合性判定": "待確認", "差異/進步性說明": ""}
        ]
        st.session_state["last_oa_result"] = None
        st.session_state["last_fetched_patent"] = None
        st.info("已清空所有欄位。")
        st.rerun()

with st.sidebar.expander("📖 操作手冊與使用說明", expanded=False):
    st.markdown(USER_MANUAL_MARKDOWN)

# ==============================================================================
# 正式宣告四大主要 Tab (確保於各 Tab 使用前完成賦值)
# ==============================================================================
tab_patent, tab_trade_secret, tab_trademark, tab_laws = st.tabs([
    "📄 專利檢索與 Claims 比對矩陣 (含化學配方)",
    "🔐 營業秘密 vs. 專利策略矩陣",
    "🏷️ 商標權佈局與圖樣生成器",
    "⚖ 智財法規速查 (專利/商標/營業秘密)"
])

# ==============================================================================
# TAB 1: 專利權模組
# ==============================================================================
with tab_patent:
    st.subheader("1. 發明標的名稱與 AI 自動拆解")

    history_opts = ["-- 📜 從已查詢項目快選 --"] + st.session_state["search_history"]
    sel_hist = st.selectbox("📜 已查詢紀錄快選（點選立即帶入）：", options=history_opts, index=0)
    if sel_hist != "-- 📜 從已查詢項目快選 --" and sel_hist != st.session_state["target_title_key"]:
        st.session_state["target_title_key"] = sel_hist
        st.rerun()

    col_input1, col_input2 = st.columns([3, 1])
    with col_input1:
        st.text_input("請輸入專利標的名稱：", key="target_title_key", placeholder="例如：高韌性熱塑性碳纖維自行車車架成型技術")
        st.checkbox("🧪 本案為化學/配方/材料組成物發明 (啟動組分配比與協同增效特化拆解)", key="is_chem_key")

    with col_input2:
        st.write("")
        st.write("")
        ai_btn = st.button("✨ Gemini AI 自動拆解", type="secondary", use_container_width=True)

    if ai_btn:
        curr_title = st.session_state["target_title_key"].strip()
        if not user_api_key.strip():
            st.error("請先於左側側邊欄輸入有效的 Gemini API Key！")
        elif not curr_title:
            st.warning("請先輸入專利標的名稱。")
        else:
            with st.spinner(f"🤖 正在為【{curr_title}】實時分析技術特徵與 Claims 要件..."):
                try:
                    ai_res = analyze_patent_with_gemini(
                        user_api_key.strip(),
                        curr_title,
                        is_chemical=st.session_state["is_chem_key"]
                    )

                    if curr_title not in st.session_state["search_history"]:
                        st.session_state["search_history"].insert(0, curr_title)

                    # 實時動態數據直接覆寫 Session State
                    st.session_state["ipc_key"] = ai_res.get("ipc", "")
                    st.session_state["cpc_key"] = ai_res.get("cpc", "")

                    st.session_state["p1_n_key"] = ai_res.get("pillar_a_name", f"Target: {curr_title}")
                    st.session_state["p1_e_key"] = ai_res.get("pillar_a_en", "")
                    st.session_state["p1_z_key"] = ai_res.get("pillar_a_zh", "")

                    st.session_state["p2_n_key"] = ai_res.get("pillar_b_name", "Mechanism: 核心構件/手段")
                    st.session_state["p2_e_key"] = ai_res.get("pillar_b_en", "")
                    st.session_state["p2_z_key"] = ai_res.get("pillar_b_zh", "")

                    st.session_state["p3_n_key"] = ai_res.get("pillar_c_name", "Effect: 技術功效與特性")
                    st.session_state["p3_e_key"] = ai_res.get("pillar_c_en", "")
                    st.session_state["p3_z_key"] = ai_res.get("pillar_c_zh", "")

                    if ai_res.get("claim_elements"):
                        st.session_state["claims_data"] = ai_res.get("claim_elements")

                    st.session_state["table_reset_counter"] += 1
                    st.success(f"🎉 已完成【{curr_title}】之專屬分析！支柱與 Claims 已實時更新！")
                    st.rerun()

                except Exception as e:
                    st.error(f"❌ AI 呼叫或分析失敗，原因：{e}")

    col_class1, col_class2 = st.columns(2)
    with col_class1:
        st.text_input("IPC 分類號 (逗號隔開)", key="ipc_key", placeholder="例: B62K 3/02, B29C 70/46")
    with col_class2:
        st.text_input("CPC 分類號 (逗號隔開)", key="cpc_key", placeholder="例: B62K 3/02, B29C 70/46")

    st.markdown("---")
    st.subheader("2. 技術特徵三支柱展開")

    col_p1, col_p2, col_p3 = st.columns(3)
    with col_p1:
        st.markdown("#### 支柱 A：應用標的 / 母材 (Target)")
        st.text_input("支柱 A 名稱", key="p1_n_key")
        st.text_area("英文關鍵字 (逗號隔開)", key="p1_e_key", height=100)
        st.text_area("中文關鍵字 (逗號隔開)", key="p1_z_key", height=100)

    with col_p2:
        st.markdown("#### 支柱 B：核心組分 / 手段 (Components)")
        st.text_input("支柱 B 名稱", key="p2_n_key")
        st.text_area("英文關鍵字 (逗號隔開)", key="p2_e_key", height=100)
        st.text_area("中文關鍵字 (逗號隔開)", key="p2_z_key", height=100)

    with col_p3:
        st.markdown("#### 支柱 C：技術功效 / 物性 (Property)")
        st.text_input("支柱 C 名稱", key="p3_n_key")
        st.text_area("英文關鍵字 (逗號隔開)", key="p3_e_key", height=100)
        st.text_area("中文關鍵字 (逗號隔開)", key="p3_z_key", height=100)

    st.markdown("---")
    st.subheader("3. 引證前案專利號爬取與自動比對 (Auto-fetch Prior Art)")
    col_fetch1, col_fetch2, col_fetch3 = st.columns([2, 1, 1])
    with col_fetch1:
        target_pno = st.text_input("前案專利號 (公開號/公告號)：", placeholder="例如：US11578418B2", key="fetch_pno_input")
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
            st.error("自動特徵拆解需要 Gemini API Key，請先於左側側邊欄填入！")
        else:
            with st.spinner(f"🌐 正在爬取 {target_pno.strip()} 並啟動比對引擎..."):
                try:
                    p_data = fetch_patent_data_from_google(target_pno.strip())
                    st.session_state["last_fetched_patent"] = p_data
                    curr_claims = st.session_state["claims_data"]

                    ai_mappings = map_prior_art_with_gemini(user_api_key.strip(), curr_claims, p_data, target_slot)
                    for idx, row in enumerate(curr_claims):
                        if idx < len(ai_mappings):
                            row[target_slot] = f"[{p_data['patent_no']}] " + ai_mappings[idx].get("matched_tech", "")
                            if ai_mappings[idx].get("judgment"):
                                row["符合性判定"] = ai_mappings[idx].get("judgment")
                            if ai_mappings[idx].get("diff_note"):
                                row["差異/進步性說明"] = ai_mappings[idx].get("diff_note")

                    st.session_state["claims_data"] = curr_claims
                    st.session_state["table_reset_counter"] += 1
                    st.success(f"✅ 成功擷取專利：【{p_data['patent_no']}】{p_data['title']}，已完成比對！")
                    st.rerun()
                except Exception as e:
                    st.error(f"爬取或比對失敗: {e}")

    st.markdown("---")
    st.subheader("4. 申請專利範圍全要件比對矩陣 (線上編輯)")

    edited_df = st.data_editor(
        pd.DataFrame(st.session_state["claims_data"]),
        num_rows="dynamic",
        use_container_width=True,
        column_config={
            "要件編號": st.column_config.TextColumn("要件編號", width="small", required=True),
            "本案 Claim 1 技術要件": st.column_config.TextColumn("本案 Claim 1 技術要件 / 配方成分", width="medium"),
            "前案 D1 對應技術": st.column_config.TextColumn("前案 D1 對應技術", width="medium"),
            "前案 D2 對應技術": st.column_config.TextColumn("前案 D2 對應技術", width="medium"),
            "符合性判定": st.column_config.SelectboxColumn("符合性判定", options=["YES (字面讀取)", "NO (不符/差異點)", "均等成立 (DOE)", "待確認"], width="small"),
            "差異/進步性說明": st.column_config.TextColumn("差異分析 / 數值臨界性 / 突變協同功效", width="large"),
        },
        key=f"claims_editor_{st.session_state['table_reset_counter']}"
    )

    col_claim_oa1, col_claim_oa2 = st.columns([2, 1])
    with col_claim_oa1:
        st.caption("💡 提示：若表格中有被判定為「NO (不符/差異點)」之配方或特徵，可利用右方按鈕一鍵撰寫《專利法》第22條進步性申復理由。")
    with col_claim_oa2:
        quick_oa_btn = st.button("⚖️ 一鍵生成《專利法》第22條進步性申復理由", use_container_width=True)

    if quick_oa_btn:
        if not user_api_key.strip():
            st.error("請先於左側側邊欄輸入有效的 Gemini API Key！")
        else:
            with st.spinner("🤖 正在提煉差異點與突變功效，撰寫專利法第22條進步性理由書..."):
                diff_rows = []
                for _, r in edited_df.iterrows():
                    elem = r.get("要件編號", "")
                    claim_desc = r.get("本案 Claim 1 技術要件", "")
                    d1_desc = r.get("前案 D1 對應技術", "")
                    diff_note = r.get("差異/進步性說明", "")
                    diff_rows.append(f"【{elem}】本案要件：{claim_desc}；前案技術：{d1_desc}；差異與進步功效：{diff_note}")

                diff_summary = "\n".join(diff_rows)
                rejection_summary = "審查官認為本案 Claim 1 所請特徵已被先前技術個別教示，為通常知識者之常規試誤，欠缺進步性。"
                
                try:
                    quick_oa_res = generate_oa_response_with_gemini(
                        user_api_key.strip(),
                        "專利法第 22 條第 2 項（進步性核駁 - 突出技術特徵與顯著功效）",
                        st.session_state["target_title_key"] if st.session_state["target_title_key"] else "本發明專利申請案",
                        rejection_summary,
                        diff_summary
                    )
                    st.session_state["last_oa_result"] = quick_oa_res
                    st.success("🎉 進步性申復理由書產生完成！")
                    st.rerun()
                except Exception as e:
                    st.error(f"生成答辯書失敗: {e}")

    if st.session_state.get("last_oa_result"):
        with st.expander("📄 檢視最新產出之專利申復答辯理由書", expanded=True):
            oa_display_text = st.session_state["last_oa_result"]
            st.text_area("申復理由書全文：", value=oa_display_text, height=350)
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
    include_effect = st.checkbox("🔍 Google 檢索式納入技術功效詞（Pillar C）", value=False)

    if st.button("🚀 生成專利檢索式並整合比對報告", type="primary", use_container_width=True):
        builder = PatentSearchBuilder(st.session_state["target_title_key"] if st.session_state["target_title_key"] else "未命名技術標的")
        if st.session_state["ipc_key"]:
            builder.add_ipc(st.session_state["ipc_key"])
        if st.session_state["cpc_key"]:
            builder.add_cpc(st.session_state["cpc_key"])

        builder.add_pillar(st.session_state["p1_n_key"], st.session_state["p1_e_key"].split(",") if st.session_state["p1_e_key"] else [], st.session_state["p1_z_key"].split(",") if st.session_state["p1_z_key"] else [])
        builder.add_pillar(st.session_state["p2_n_key"], st.session_state["p2_e_key"].split(",") if st.session_state["p2_e_key"] else [], st.session_state["p2_z_key"].split(",") if st.session_state["p2_z_key"] else [])
        builder.add_pillar(st.session_state["p3_n_key"], st.session_state["p3_e_key"].split(",") if st.session_state["p3_e_key"] else [], st.session_state["p3_z_key"].split(",") if st.session_state["p3_z_key"] else [])

        google_query = builder.to_google_patents_query(include_effect_pillar=include_effect)
        gpss_query = builder.to_gpss_query(include_effect_pillar=True)
        
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
            st.markdown("#### 🇹🇼 台灣智慧局專利檢索系統 (GPSS / TWPAT) 檢索式")
            st.code(gpss_query if gpss_query else "（無有效檢索式）", language="text")
            if gpss_query.strip():
                render_copy_button(gpss_query, "📋 快速複製 GPSS 檢索式", button_id="copyGPSS")
                col_link_a, col_link_b = st.columns(2)
                with col_link_a:
                    st.link_button("🇹🇼 全球專利檢索系統 (GPSS)", "https://tiponet.tipo.gov.tw/gpss/", type="secondary", use_container_width=True)
                with col_link_b:
                    st.link_button("🇹🇼 專利資訊檢索 (TWPAT)", "https://tiponet.tipo.gov.tw/twpat/", type="secondary", use_container_width=True)

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

        with col_dl2:
            st.download_button(
                "📊 下載前案比對矩陣 (.csv)",
                data=csv_bytes,
                file_name=f"claim_chart_{time_str}.csv",
                mime="text/csv",
                type="secondary",
                use_container_width=True
            )

# ==============================================================================
# TAB 2: 營業秘密 vs. 專利策略佈局模組
# ==============================================================================
with tab_trade_secret:
    st.subheader("🔐 營業秘密 vs. 專利策略佈局決策矩陣 (Patent vs. Trade Secret Decision Matrix)")
    col_ts_eval1, col_ts_eval2 = st.columns(2)
    with col_ts_eval1:
        st.markdown("#### 1. 技術特性與外部逆向工程難易度")
        score_re = st.slider("反向工程（Reverse Engineering）難度：", min_value=1, max_value=5, value=3)
        score_detect = st.slider("市場侵權可偵測性（Detectability of Infringement）：", min_value=1, max_value=5, value=2)
        score_lifecycle = st.slider("產品/技術市場生命週期（Market Life Cycle）：", min_value=1, max_value=5, value=4)

    with col_ts_eval2:
        st.markdown("#### 2. 製程特性與企業內部管控力")
        score_process = st.slider("技術核心偏向製程操作 vs. 終端成品：", min_value=1, max_value=5, value=4)
        score_protection = st.slider("企業內部合理保密措施完備程度：", min_value=1, max_value=5, value=3)

    ts_weighted_score = (score_re * 0.25) + ((6 - score_detect) * 0.25) + (score_lifecycle * 0.15) + (score_process * 0.20) + (score_protection * 0.15)

    st.markdown("---")
    col_res_ts1, col_res_ts2 = st.columns([1, 2])
    with col_res_ts1:
        st.metric(label="營業秘密傾向指數", value=f"{ts_weighted_score:.2f} / 5.0")
        if ts_weighted_score >= 3.6:
            st.success("🎯 **強烈建議：封存為【營業秘密】保護**")
        elif ts_weighted_score >= 2.8:
            st.warning("⚖️ **雙軌佈局：【專利 ＋ 營業秘密】混合防禦**")
        else:
            st.info("📄 **強烈建議：全面申請【發明專利】公開排他**")

    with col_res_ts2:
        if ts_weighted_score >= 3.6:
            st.write("反向工程門檻極高且侵權蒐證不易，建議依《營業秘密法》第2條落實門禁、代號與NDA合理保密措施。")
        elif ts_weighted_score >= 2.8:
            st.write("終端主要組分與廣泛配比申請專利，製程最佳黃金參數（Know-how）則保留為內部營業秘密。")
        else:
            st.write("外部極易逆向解析還原，請立即申請發明專利，建立專利排他權。")

# ==============================================================================
# TAB 3: 商標權模組
# ==============================================================================
with tab_trademark:
    st.subheader("🏷️️ 商標尼斯分類佈局與 TIPO 規範圖樣產生器")
    col_tm1, col_tm2 = st.columns([1, 1])

    with col_tm1:
        st.markdown("#### 1. 品牌標的與產品資訊")
        tm_brand = st.text_input("擬申請商標文字 (中/英文)：", value="極塑 PolyUltra")
        tm_desc = st.text_area("產品或服務技術概述：", value="用於先進複合材料、自行車或電子部件之強化構件。", height=100)

        if st.button("✨ 執行商標識別性與尼斯分類 AI 評估", type="secondary", use_container_width=True):
            if not user_api_key.strip():
                st.error("請先於左側側邊欄輸入有效的 Gemini API Key！")
            elif not tm_brand.strip():
                st.warning("請填寫擬申請之商標文字。")
            else:
                with st.spinner("🤖 正在評估商標識別性與分類..."):
                    try:
                        st.session_state["tm_analysis"] = analyze_trademark_with_gemini(user_api_key.strip(), tm_brand.strip(), tm_desc.strip())
                        st.success("🎉 商標分析完成！")
                    except Exception as e:
                        st.error(f"商標分析失敗: {e}")

        if st.session_state.get("tm_analysis"):
            res = st.session_state["tm_analysis"]
            st.info(f"**識別性等級**：{res.get('distinctiveness_level', '未知')}\n\n**審查評語**：{res.get('legal_risk_analysis', '')}")
            st.link_button("🇹🇼 開啟經濟部智慧局商標檢索首頁", "https://twtmsearch.tipo.gov.tw/", use_container_width=True)

    with col_tm2:
        st.markdown("#### 2. TIPO 電子送件商標圖樣即時產生器")
        tm_multiline_text = st.text_area("圖樣文字內容：", value=tm_brand.strip() if tm_brand.strip() else "極塑\nPolyUltra", height=75)
        uploaded_logo = st.file_uploader("選填：上傳品牌 Logo 圖檔", type=["png", "jpg", "jpeg"])

        col_ctrl1, col_ctrl2 = st.columns(2)
        with col_ctrl1:
            layout_options = ["純文字模式"]
            if uploaded_logo is not None:
                layout_options = ["複合商標：上圖下文", "複合商標：左圖右文"] + layout_options
            layout_choice = st.selectbox("圖樣排版方式：", layout_options)
        with col_ctrl2:
            align_choice = st.selectbox("文字對齊方式：", ["置中對齊", "靠左對齊", "靠右對齊"])

        font_size_val = st.slider("文字大小：", min_value=28, max_value=120, value=68, step=2)

        if tm_multiline_text.strip():
            img_bytes = create_tipo_trademark_bytes(
                text=tm_multiline_text.strip(),
                layout=layout_choice,
                font_size=font_size_val,
                text_align=align_choice,
                logo_file=uploaded_logo
            )
            st.image(img_bytes, caption="📸 圖樣預覽 (8x8 cm @ 300 DPI 標準白底)", width=320)
            st.download_button(
                label="📥 下載標準商標圖樣檔 (.jpg)",
                data=img_bytes,
                file_name="trademark_spec.jpg",
                mime="image/jpeg",
                type="primary",
                use_container_width=True
            )

# ==============================================================================
# TAB 4: 智財法規速查
# ==============================================================================
with tab_laws:
    st.subheader("⚖️️ 專利法、商標法與營業秘密法 關鍵條文指南")
    for item in IP_LAWS_DB:
        with st.expander(f"⚖️ {item['article']}：{item['title']}"):
            st.code(item["text"], language="text")
            st.info(item["explanation"])
