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
# 一、 核心資料結構與專利檢索邏輯 (含分號過濾、分類號正規化與防過度限縮機制)
# ==============================================================================
@dataclass
class TechnicalPillar:
    """定義單一技術支柱（名稱、英文關鍵字、中文關鍵字）"""
    name: str
    en_keywords: List[str] = field(default_factory=list)
    zh_keywords: List[str] = field(default_factory=list)

class PatentSearchBuilder:
    """專利檢索邏輯式建造器 (含分號過濾、佔位符黑名單清洗與分類號規範防禦)"""

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
        """清洗純文字，徹底剔除分號、換行與多餘空白"""
        if not text:
            return ""
        cleaned = re.sub(r'[;\r\n]+', ' ', str(text))
        cleaned = re.sub(r'\s+', ' ', cleaned).strip()
        return cleaned

    @classmethod
    def _clean_keyword(cls, kw: str) -> str:
        """清洗單一關鍵字或片語"""
        cleaned = cls._clean_text(kw).strip('",\'')
        if not cleaned or cleaned.lower() in cls.PLACEHOLDER_BLACKLIST:
            return ""
        return cleaned

    @classmethod
    def _normalize_class_code(cls, code: str) -> str:
        """正規化 IPC/CPC 分類號（移除空格、非法分號與重複前綴）"""
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
        """產生符合 Google Patents 規範之扁平化布林檢索式（末尾絕不帶分號）"""
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
        """產生符合台灣智慧局 GPSS 格式之檢索式"""
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
            f"▶ 台灣智慧財產局 (GPSS) 檢索語法：",
            f"{self.to_gpss_query(include_effect_pillar=True)}",
            f"\n" + "=" * 85,
            f"【四、申請專利範圍（Claims）合規檢核表（含化學配方專屬要項）】",
            f"=" * 85,
            f"[ ] 1. 標的定性清楚：獨立項前言（Preamble）是否清楚載明法定標的類型（如：組成物/添加劑/製法/用途）？",
            f"[ ] 2. 開閉鎖過渡詞：是否善用「包含（comprising，開放式）」或「由...組成（consisting of，閉鎖式）」防禦替代成分？",
            f"[ ] 3. 組分與數值臨界性：配方比例（重量份、wt%、莫耳比）是否界定明確且在說明書中有臨界功效數據（Criticality）佐證？",
            f"[ ] 4. 馬庫西（Markush）格式：選擇性群組是否為具相似化學結構或同族功能之均等物，避免過度廣泛觸發可據以實現瑕疵？",
            f"[ ] 5. 獨立項最小特徵原則：獨立項是否只保留達成核心相乘增效之必要成分，將次要助劑保留於附屬項？",
            f"[ ] 6. 名詞前置依據（Antecedent Basis）：所有冠上「該（said/the）」之成分與參數，先前是否有一致之首次引入定義？",
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
            f"1. 新穎性/字面讀取：引證案是否完全揭露本案所有特徵與組分配比範圍？",
            f"2. 進步性突變功效：本案特定成分比例是否具無法預期之協同增效（Synergistic Effect）？",
            "=" * 85
        ])

        if prior_art_data:
            lines.extend([
                f"\n【六、引證前案原文摘錄（附錄 Appendix）】",
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
# 三、 Gemini AI 自動重試與指數退避輪替 (高可用抗 503/429 強化版)
# ==============================================================================
CANDIDATE_MODELS = [
    "gemini-2.5-flash",        # 首選：高吞吐低延遲
    "gemini-2.0-flash",        # 次選：官方長期穩定版本
    "gemini-2.5-pro",          # 備援：高階深度推理版本
    "gemini-1.5-flash",        # 備援：吞吐量最大、負載容忍度高
    "gemini-1.5-pro"           # 終極備援
]

def _extract_json_from_text(raw_text: str):
    """當模型無法直接輸出純 JSON 時，容錯抽取 Markdown 或文字中的 JSON 區塊"""
    match = re.search(r'```(?:json)?\s*([\s\S]*?)\s*```', raw_text)
    if match:
        return json.loads(match.group(1))
    
    curly_match = re.search(r'(\{[\s\S]*\}|\[[\s\S]*\])', raw_text)
    if curly_match:
        return json.loads(curly_match.group(1))
        
    return json.loads(raw_text)

def generate_with_fallback(client, prompt: str, as_json: bool = True) -> str:
    """
    跨多模型輪替與階梯式退避保護：
    - 遭遇 503 UNAVAILABLE / 429 限流時，動態拉長等待並快速輪替下一個備援模型
    - 遭遇 404 NOT_FOUND（版本無效）時，立刻切換至下一版本
    """
    last_exception = None

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
                last_exception = e
                err_str = str(e)

                if "404" in err_str or "NOT_FOUND" in err_str:
                    break

                if any(code in err_str for code in ["503", "429", "UNAVAILABLE", "RESOURCE_EXHAUSTED", "high demand"]):
                    wait_time = 2.5 * (attempt + 1)
                    time.sleep(wait_time)
                    continue

                if as_json and ("response_mime_type" in err_str or "json" in err_str.lower()):
                    try:
                        fallback_resp = client.models.generate_content(
                            model=model_name,
                            contents=prompt + "\n\n【重要：請務必以純 JSON 格式回傳，不要附加額外說明】"
                        )
                        if fallback_resp and fallback_resp.text:
                            return fallback_resp.text
                    except Exception:
                        pass
                
                break

    raise last_exception if last_exception else Exception("所有備援模型皆處於尖峰繁忙狀態，請稍候 30 秒重試，或自側邊欄載入現成範本。")

def analyze_patent_with_gemini(api_key: str, title: str, is_chemical: bool = False) -> dict:
    """專利特徵與 IPC/CPC 拆解（支援一般機械電子與化學配方發明）"""
    client = genai.Client(api_key=api_key)
    
    chem_instructions = ""
    if is_chemical:
        chem_instructions = """
        【本案為化學/材料/配方/組成物專利】：
        1. 支柱 A (Target) 需包含目標材料/化學組成物名稱、應用領域（如封裝膠、電鍍浴、催化劑）。
        2. 支柱 B (Mechanism/Components) 請聚焦於關鍵核心化學組分、官能基、聚合物架構、共聚單體或添加劑類別。
        3. 支柱 C (Effect/Property) 需列出物理/化學物性增益（如 Tg溫度、低介電常數 Dk/Df、抗剝離強度、協同增效、耐高溫）。
        4. 拆解之 Claim 1 要件需嚴格區分主要成分(A)、次要/改質成分(B)、重量配比範圍(C)、製程條件或相乘協同功效(D)。
        """

    prompt = f"""
    你是一名專業的專利代理人與資深專利檢索專家。
    請分析以下發明專利標的名稱，並以繁體中文與專業英文進行技術三支柱拆解、分類號建議與 Claim 1 要件拆解。
    {chem_instructions}

    發明標的名稱："{title}"

    請嚴格依照以下 JSON 結構回傳：
    {{
        "ipc": "建議的 IPC 分類號，用逗號隔開 (如 C08L 63/00, C08K 3/36, C25D 3/46)",
        "cpc": "建議的 CPC 分類號，用逗號隔開",
        "pillar_a_name": "Target: 標的或材料組成物名稱",
        "pillar_a_en": "英文關鍵字5~7個，逗號隔開",
        "pillar_a_zh": "中文同義詞5~7個，逗號隔開",
        "pillar_b_name": "Mechanism/Components: 核心組分/官能基/技術手段",
        "pillar_b_en": "英文關鍵字5~7個，逗號隔開",
        "pillar_b_zh": "中文同義詞5~7個，逗號隔開",
        "pillar_c_name": "Effect/Property: 技術功效與物化性能",
        "pillar_c_en": "英文關鍵字5~7個，逗號隔開",
        "pillar_c_zh": "中文同義詞5~7個，逗號隔開",
        "claim_elements": [
            {{
                "要件編號": "Element 1A",
                "本案 Claim 1 技術要件": "具體構件描繪或核心成分界定",
                "前案 D1 對應技術": "",
                "前案 D2 對應技術": "",
                "符合性判定": "待確認",
                "差異/進步性說明": "預估發明點或突變協同功效說明"
            }}
        ]
    }}
    """
    res_text = generate_with_fallback(client, prompt, as_json=True)
    return _extract_json_from_text(res_text)

def map_prior_art_with_gemini(api_key: str, current_elements: list, prior_art_data: dict, target_col: str) -> list:
    """使用 Gemini 比對引證案內容與本案要件"""
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
                "class_num": "第 01 類 或 第 09 類等",
                "group_codes": "分類組群碼",
                "recommended_items": "具體建議指定商品項目"
            }}
        ],
        "clearance_search_keywords": "建議於 TIPO 檢索時比對的文字或同音異字 (逗號隔開)"
    }}
    """
    res_text = generate_with_fallback(client, prompt, as_json=True)
    return _extract_json_from_text(res_text)

def generate_oa_response_with_gemini(api_key: str, law_article: str, target_name: str, rejection_grounds: str, diff_facts: str) -> str:
    """自動撰寫智財局核駁審查意見申復理由書草稿"""
    client = genai.Client(api_key=api_key)
    prompt = f"""
    你是一名台灣資深專利代理人與商標/營業秘密智財律師。
    請依據台灣《經濟部智慧財產局（TIPO）》官方審查基準與法定申復書規格，針對下列核駁審查意見通知函（Office Action）或爭議指控，撰寫一份結構嚴謹、條理分明、具高度法理說服力的【申復/答辯理由書（草稿）】。

    【引用法條/爭議事由】：{law_article}
    【本案標的名稱/對造商標】：{target_name}
    【核駁或指控理由摘要】：{rejection_grounds}
    【申請人/答辯人主張之實體差異事實與論據】：{diff_facts}

    若涉及化學/配方專利進步性，請務必融入：
    1. 避免事後諸葛（Hindsight Bias），先前技術未提供將特定組分以特定重量比結合之動機或啟示（No Teaching / Suggestion / Motivation）。
    2. 強調數值範圍的臨界性（Criticality of Numerical Range）與非顯而易見的突變協同增效（Synergistic Effect）。
    3. 引證案若有相反教示（Teaching Away）或容易劣化之阻礙，予以強力反駁。

    請使用正式專利法律繁體中文撰寫，包含：
    一、案由與前言聲明
    二、法規意旨與審查基準法理依據
    三、爭點具體比對與實體答辯理由
    四、結論與懇請事項
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
        canvas.paste(resized_logo, (start_x, logo_y))

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
# 六、 智財核心法規資料庫 (專利法、商標法、營業秘密法)
# ==============================================================================
IP_LAWS_DB = [
    {
        "category": "營業秘密法",
        "article": "營業秘密法 第 2 條",
        "title": "營業秘密之法定三要件",
        "keywords": "營業秘密, 秘密性, 經濟價值, 合理保密措施, 配方, 製程",
        "text": (
            "本法所稱營業秘密，指方法、技術、製程、配方、程式、設計或其他可用於生產、銷售或經營之資訊，而符合下列要件者：\n"
            "一、非一般涉及該類資訊之人所知者（秘密性）。\n"
            "二、因其秘密性而具有實際或潛在之經濟價值者（經濟價值性）。\n"
            "三、所有人已採取合理之保密措施者（合理保密措施）。"
        ),
        "explanation": "【實務精要】：化學配方若不公開申請專利，欲以營業秘密法保護，必須「嚴格具備」三要件。尤其是第三款「合理保密措施」，企業必須建置門禁管制、NDA簽署、權限分級、標示機密標籤及加密儲存，否則訴訟時將被法院直接判定不構成營業秘密而全面敗訴。"
    },
    {
        "category": "營業秘密法",
        "article": "營業秘密法 第 10 條",
        "title": "侵害營業秘密之行為態樣",
        "keywords": "侵權, 不正方法, 洩漏, 違背保密義務, 竊取",
        "text": (
            "有下列情形之一者，為侵害營業秘密：\n"
            "一、以竊取、毀損、脅迫、詐術或其他不正當方法取得營業秘密，或取得後進而使用、洩漏者。\n"
            "二、知悉或因重大過失而不知其為前款之營業秘密，而取得、使用或洩漏者。\n"
            "三、持有營業秘密，無正當理由而洩漏或使用，或違背維持營業秘密之義務者。\n"
            "四、因法律行為取得營業秘密，而以不正當方法使用或洩漏者。"
        ),
        "explanation": "【實務精要】：反向工程（Reverse Engineering）自市場合法購得成品並拆解還原成分者，屬於合法技術獲取，不構成侵權！因此若配方極易被對手化驗解析還原，切勿僅仰賴營業秘密，應儘速申請專利防禦。"
    },
    {
        "category": "營業秘密法",
        "article": "營業秘密法 第 11 條",
        "title": "侵害營業秘密之民事救濟（排除與防止請求權）",
        "keywords": "排除侵害, 防止侵害, 銷毀, 損害賠償",
        "text": (
            "營業秘密受侵害時，所有人得請求排除之；有侵害之虞者，得請求防止之。\n"
            "營業秘密所有人請求排除或防止侵害時，得請求銷毀、返還或為其他必要處分，包括侵害行為所製造之成品或供侵害行為所用之物。"
        ),
        "explanation": "【實務精要】：原告可聲請法院查扣銷毀對手仿冒生產之化學原料、產線模具與中間體，並防止其進一步進入市場流通。"
    },
    {
        "category": "營業秘密法",
        "article": "營業秘密法 第 13 條之 1 / 第 13 條之 2",
        "title": "侵害營業秘密罪之刑事責任（境內與境外加重刑責）",
        "keywords": "刑事責任, 有期徒刑, 罰金, 意圖域外使用, 國安",
        "text": (
            "意圖為自己或第三人不法之利益，或損害營業秘密所有人之利益，而有下列情形之一，處五年以下有期徒刑或拘役，得併科新臺幣一百萬元以上一千萬元以下罰金：\n"
            "一、以竊取、侵占、詐術、脅迫、未經授權而重製或其他不正方法而取得營業秘密，或取得後進而使用、洩漏者。\n"
            "二、知悉或因重大過失而不知為前款之營業秘密，而取得、使用或洩漏者。\n"
            "三、持有營業秘密，未經授權或逾越授權範圍而重製、使用或洩漏該營業秘密者。\n\n"
            "【第 13 條之 2 意圖在境外使用者】：\n"
            "意圖在外國、大陸地區、香港或澳門使用，而犯前條第一項各款之罪者，處一年以上十年以下有期徒刑，得併科新臺幣三百萬元以上五千萬元以下罰金。"
        ),
        "explanation": "【實務精要】：帶走公司配方跳槽境外對手，適用第 13-2 條境外加重處罰，刑度高達 1 年以上 10 年以下有期徒刑，屬重罪案件，檢調可依法實施境管與強制搜索扣押。"
    },
    {
        "category": "專利法",
        "article": "專利法 第 21 條",
        "title": "發明之定義",
        "keywords": "自然法則, 技術思想, 發明, 化合物, 配方",
        "text": "本法所稱發明，指利用自然法則之技術思想之創作。",
        "explanation": "化學物質、分子結構、聚合物改質、配方添加劑、物理混合物及其製備方法，皆屬利用自然法則之法定發明標的。"
    },
    {
        "category": "專利法",
        "article": "專利法 第 22 條",
        "title": "專利三要件（產業利用性、新穎性、進步性）",
        "keywords": "新穎性, 進步性, 產業利用性, 協同增效, 數值限定",
        "text": (
            "可供產業上利用之發明，無下列情事之一，得依本法申請專利：\n"
            "一、申請前已見於刊物者。\n"
            "二、申請前已公開實施者。\n"
            "三、申請前已為公眾所知悉者。\n\n"
            "發明雖無前項各款所列情事，但為其所屬技術領域中具有通常知識者依申請前之先前技術所能輕易完成時，仍不得依本法申請專利。"
        ),
        "explanation": "【化學配方審查實務】：成分組合若僅為已知助劑的簡單疊加，欠缺進步性；但若配方比例產生超出預期之「突變協同功效（Synergistic Effect）」，或特定數值區間內物性產生不可預期躍升，即具備法定進步性。"
    },
    {
        "category": "專利法",
        "article": "專利法 第 26 條",
        "title": "說明書充分揭露與請求項明確性（化學配方可據以實現要件）",
        "keywords": "說明書, 實施例, 可據以實現, 明確性, 支持",
        "text": (
            "說明書應明確且充分揭露，使該發明所屬技術領域中具有通常知識者，能瞭解其內容，並可據以實現。\n"
            "申請專利範圍應界定申請專利之發明；其得包括一項以上之請求項，各請求項應以明確、簡潔之方式記載，且必須為說明書所支持。"
        ),
        "explanation": "【化學配方專屬致命點】：配方專利極度注重實施例與比較例。若獨立項寫得太寬（如 1~99 wt%），而實施例僅有一組且無充分物性數據證明全範圍皆可行，審查官常依第 26 條判定「無法據以實現」或「申請專利範圍未受說明書支持」而直接核駁。"
    },
    {
        "category": "專利法",
        "article": "專利法 第 58 條",
        "title": "專利權人之排他專有權限",
        "keywords": "專利權, 排他權, 製造, 販賣, 進口, 方法專利推定",
        "text": (
            "專利權人，除本法另有規定外，專有排除他人未經其同意而製造、為販賣之要約、販賣、使用或為上述目的而進口該發明之權。\n"
            "物之發明，其專利權範圍不及於以該物為標的所生產之產品。"
        ),
        "explanation": "化學配方可同時佈局「物質/組成物（Composition of Matter）」項與「製法（Process）」項，享受物之專利全面排他與方法專利在國內外之邊境查扣效益。"
    },
    {
        "category": "商標法",
        "article": "商標法 第 18 條",
        "title": "商標之定義與識別性基本原則",
        "keywords": "商標, 識別性, 表彰, 商品, 服務",
        "text": "商標，指任何具有識別性之標識，得以文字、圖形、記號、顏色、立體形狀等組成。",
        "explanation": "商標為指示商品或服務來源之表彰標識。"
    },
    {
        "category": "商標法",
        "article": "商標法 第 30 條 第 1 項 第 10 款",
        "title": "相對不得註冊事由（混淆誤認之虞）",
        "keywords": "混淆誤認, 相同, 近似, 先申請",
        "text": "相同或近似於他人同一或類似商品或服務之註冊商標或申請在先之商標，有致相關消費者混淆誤認之虞者，不得註冊。",
        "explanation": "商標審查之核心條款，依圖樣近似、商品類似、購買者注意程度綜合評斷。"
    }
]

USER_MANUAL_MARKDOWN = """# 📖 智慧財產權整合工作台 操作手冊

---

## 🚀 準備作業：設定 API 金鑰
1. 開啟工作台首頁，查看螢幕**左側側邊欄（Sidebar）**。
2. 在 **「🔑 Gemini API 設定」** 欄位貼入您的 Google Gemini API Key。

---

## 模組一：📄 專利檢索與 Claims 比對矩陣 (含化學配方發明專利特化)

### 步驟 1：標的名稱與 AI 特徵拆解
* **化學配方模式開關**：若發明屬於化學、材料、添加劑、聚合物或組成物，請勾選「🧪 本案為化學/配方/材料組成物發明」。AI 將特別針對**「組分官能基、配方重量比例、物化特性與協同增效」**進行專門解構。
* **技術範本一鍵載入**：可在側邊欄選取範本後點擊「📥 載入範本」快速帶入；若要手動全新輸入，隨時點擊「🧹 清空所有」即可。

### 步驟 2：引證前案爬取與全要件比對
1. 輸入引證前案號碼（如：`US11578418B2`、`US6165342A`）。
2. 點擊「📥 爬取並自動填入」，AI 自動對應前案成分與本案要件，判定 YES / NO / 均等成立。

### 步驟 3：線上編輯與進步性申復
* 表格中若有不符之配比或成分特徵，點擊「⚖ 一鍵生成進步性申復理由」，AI 自動撰寫數值臨界性與突變協同功效申復書。

---

## 模組二：🔐 營業秘密 vs. 專利策略矩陣

### 步驟 1：專利 vs. 營業秘密 互動式決策矩陣
* 輸入 5 項關鍵指標（反向工程難易度、產品市場週期、侵權舉證難度、製程不可逆程度、企業保密管控力），系統自動計算策略量化指數。

### 步驟 2：營業秘密法定三要件實體檢核
* 依台灣《營業秘密法》第 2 條逐項檢視「秘密性」、「經濟價值」與「合理保密措施」。

---

## 模組三：🏷️ 商標權佈局與圖樣生成器
* 支援多行排版、對齊與行距微調，產出符合 TIPO 8×8 cm @ 300 DPI 官方標準規範 JPEG 圖檔。

---

## 模組四：⚖️ 智財法規速查 ＆ AI 申復答辯理由書產生器
* 整合《專利法》、《商標法》與《營業秘密法》（含第 13-2 條域外加重刑責）。
"""

OA_CHEM_FORMULA_DOC = """專利申復理由書（草稿）

案  號：第 [請填入申請案號] 號
申 請 人：[請填入專利申請人/公司名稱]
發明名稱：半導體先進封裝用低介電高散熱環氧樹脂填料組成物
受 文 者：經濟部智慧財產局

--------------------------------------------------------------------------------
一、 案由與前言聲明
--------------------------------------------------------------------------------
本件專利申請案業經 貴局審查官惠示審查意見通知函，認本案申請專利範圍請求項第 1 項所請之化學組成物配比特徵，為所屬技術領域中具有通常知識者結合引證案 D1 與引證案 D2 所能輕易完成，而有違反《專利法》第 22 條第 2 項（進步性）之虞。

申請人深感審查官審查之辛勞，經詳加研析前揭核駁理由與引證文獻後，謹陳明：引證案 D1 與引證案 D2 實質上並未揭露本案請求項第 1 項所特定界定之「雙環戊二烯型環氧樹脂與矽烷偶合修飾奈米球形二氧化矽之 1:2.5 至 1:4.0 特定重量比」（Element 1C），更未教示該配比能產生「高填料率下黏度驟降且介電損耗 Df 低於 0.003」之無法預期的突變協同增效（Synergistic Effect）。本案依法具備突出之技術特徵與顯著之功效增益，自具進步性。

--------------------------------------------------------------------------------
二、 審查基準法理依據
--------------------------------------------------------------------------------
按《專利審查基準》第二篇第三章第 3.4 節明確規定：
1. 「避免事後諸葛（Avoid Hindsight Bias）」：判斷化學發明進步性時，先前技術若未揭示將特定成分以特定數值區間組合之明確技術啟示或動機（Motivation），不得主觀拼湊引證案否定進步性。
2. 「無法預期之突變功效（Unexpected Technical Effect）」：化學組成物之成分配比若在特定數值臨界區間內，產生超越常規理論預測、非各組分單純性能相加之協同增益，即具備法定進步性。

--------------------------------------------------------------------------------
三、 爭點具體比對與實體答辯理由
--------------------------------------------------------------------------------
（一） 引證案未揭露本案特定配合比，且存在反向教示（Teaching Away）
1. 引證案 D1 僅揭示常規雙酚 A 型樹脂，其主鏈極性高，未曾教示採用本案特定之雙環戊二烯低介電骨架。
2. 引證案 D2 雖然揭示無機填料，但明白記載：當二氧化矽填料重量比超過 1:2 時，體系黏度將呈指數暴增而失去流動性，導致封裝產生嚴重空洞（Void）。引證案 D2 之技術教示乃是力求壓低填料比例。
3. 本案反向突破該限制，藉由特定官能基矽烷之立體阻礙設計，在 1:2.5 至 1:4.0 極高填料比下，反常性維持低黏度與高導熱，先前技術顯有反向阻礙教示。

（二） 本案特定臨界數值產生無法預期之相乘協同功效
依據本案說明書實施例 1~3 與比較例之對照數據：
1. 介電損耗（Df）與介電常數（Dk）突變下降：在 1:2.5~1:4.0 配比下，Df 驟降至 0.0028（10 GHz），較引證案 D1 之 0.015 改善達 80% 以上。
2. 熱膨脹係數（CTE）降至 10 ppm/°C 以下，且熱導率達到 2.5 W/m·K 以上，徹底克服封裝翹曲（Warpage）問題。

上述物性突破絕非通常知識者依常規常識所能預期，屬典型之突變性協同增效。

--------------------------------------------------------------------------------
四、 結論與懇請事項
--------------------------------------------------------------------------------
綜上，本案 Claim 1 具備新穎性與進步性，懇請 貴局審查官惠予早日核准審定，實感德便。

謹呈
經濟部智慧財產局 公鑒
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

申請人深感審查官審查之辛勞，經詳加研析前揭核駁理由與引證文獻後，謹陳明：引證案 D1 與引證案 D2 實質上並未揭露本案請求項第 1 項所特定界定之「主光澤劑與輔助細化劑之重量比為 1:1 至 10:1」之關鍵吸附平衡技術特徵（Element 1C），更未教示或暗示該特定配比能誘發「陰極極化過電位負移 50 至 200 mV」並將晶粒強制細化至 80 nm 以下且杜絕脆化之突變性協同增效（Synergistic Effect）。本案依法自具進步性。

--------------------------------------------------------------------------------
二、 審查基準法理依據
--------------------------------------------------------------------------------
按《專利審查基準》第二篇第三章第 3.4 節「進步性之判斷」明載：
1. 「不可事後諸葛（Avoid Hindsight Bias）」：先前技術若未提供結合之動機或啟示，即不得任意將多份引證案拼湊以否定進步性。
2. 「無法預期之技術功效（Unexpected Technical Effect）」：在數值範圍或成分配比之發明中，若發明限定之特定成分比例範圍，產生非通常知識者依既有理論所能預測之突變性增益者，即應認定具備進步性。

--------------------------------------------------------------------------------
三、 爭點具體比對與實體答辯理由
--------------------------------------------------------------------------------
本案特定之 1:1 至 10:1 重量配比突破了先前技術的限制：
1. 引證案 D1 僅為一般有機添加劑之單純教示，未限定主光澤劑與含硫成分之相互作用配比。
2. 引證案 D2 之硫脲抑制體系係採取 1:20 以上之微量添加，並教示若提高含硫添加劑濃度將導致鍍層共析脆化。通常知識者參酌 D2 之負面教示，理應避免將兩者配比維持於 1:1 至 10:1 區間。
3. 本案特定配比使陰極極化過電位大幅負移 50 至 200 mV，晶粒尺寸被抑制於 80 奈米以下，且徹底克服脆化問題，產生顯著之突變協同增效。

--------------------------------------------------------------------------------
四、 結論與懇請事項
--------------------------------------------------------------------------------
綜上，本案 Claim 1 具備進步性要件，懇請 貴局審查官惠予核准審定。

謹呈
經濟部智慧財產局 公鑒
"""

# ==============================================================================
# 七、 Streamlit 介面與 Session State 同步管理
# ==============================================================================
st.set_page_config(
    page_title="智慧財產權整合工作台 (專利 ＆ 商標 ＆ 營業秘密)",
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
    "p2_n_val": "Mechanism/Components: 核心手段/組分",
    "p2_e_val": "",
    "p2_z_val": "",
    "p3_n_val": "Effect/Property: 技術功效/物化特性",
    "p3_e_val": "",
    "p3_z_val": "",
    "is_chemical_patent": False,
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
    help="可在 Google AI Studio (aistudio.google.com) 免費申請 API Key。"
)

st.sidebar.markdown("---")
st.sidebar.header("📁 快速載入技術範本")

selected_template = st.sidebar.selectbox(
    "選擇要載入的範本：",
    [
        "半導體封裝低介電環氧樹脂 (化學配方)",
        "貴金屬電鍍晶粒細化光澤劑 (化學配方)",
        "多光譜溫室作物病害早期偵測系統"
    ],
    key="template_select_key"
)

col_tmpl1, col_tmpl2 = st.sidebar.columns(2)
with col_tmpl1:
    if st.button("📥 載入範本", use_container_width=True):
        if selected_template == "半導體封裝低介電環氧樹脂 (化學配方)":
            st.session_state["patent_title_input"] = "半導體先進封裝用低介電高散熱環氧樹脂填料組成物"
            st.session_state["is_chemical_patent"] = True
            st.session_state["ipc_input_val"] = "C08L 63/00, C08K 3/36, C08G 59/20, H01L 23/29"
            st.session_state["cpc_input_val"] = "C08L 63/00, C08K 3/36, H01L 23/295"
            st.session_state["p1_n_val"] = "Target: 先進封裝低介電樹脂"
            st.session_state["p1_e_val"] = "epoxy molding compound, underfill resin, semiconductor packaging, dielectric matrix"
            st.session_state["p1_z_val"] = "環氧模塑料, 底部填膠, 半導體封裝, 低介電基質, 覆晶封裝"
            st.session_state["p2_n_val"] = "Components: 雙環戊二烯樹脂與偶合修飾球矽"
            st.session_state["p2_e_val"] = "dicyclopentadiene epoxy, spherical silica, silane coupling agent, cyanate ester"
            st.session_state["p2_z_val"] = "雙環戊二烯環氧, 球形二氧化矽, 矽烷偶合劑, 氰酸酯, 活性硬化劑"
            st.session_state["p3_n_val"] = "Property: 低損耗與高散熱低應力"
            st.session_state["p3_e_val"] = "low dielectric dissipation Df, low CTE, thermal conductivity, high warpage resistance"
            st.session_state["p3_z_val"] = "低介電損耗 Df, 低熱膨脹係數 CTE, 高導熱率, 抑制晶圓翹曲, 耐吸濕回焊"
            st.session_state["claims_data"] = [
                {"要件編號": "Element 1A", "本案 Claim 1 技術要件": "一種低介電高散熱環氧樹脂組成物，包含 100 重量份之主樹脂基質，其中該主樹脂包含至少 40 wt% 之雙環戊二烯型（DCPD）環氧寡聚物", "前案 D1 對應技術": "常規雙酚 A 型或酚醛型環氧樹脂", "前案 D2 對應技術": "環脂族液態環氧樹脂", "符合性判定": "NO (不符/差異點)", "差異/進步性說明": "本案採用 DCPD 剛性脂環骨架，其無極性特徵大幅降低高頻微波下之偶極極化，降低介電損耗 Df 至 0.003 以下。"},
                {"要件編號": "Element 1B", "本案 Claim 1 技術要件": "包含 250 至 400 重量份之表面改質球形二氧化矽奈米粉體，以含苯基之三甲氧基矽烷預處理", "前案 D1 對應技術": "未經修飾之角狀石英粉（填料量小於 150 份）", "前案 D2 對應技術": "常規胺基矽烷修飾球矽", "符合性判定": "NO (不符/差異點)", "差異/進步性說明": "特定苯基矽烷修飾能抑制奈米團聚，於極高填充量下仍維持流動度與抗空洞特性。"},
                {"要件編號": "Element 1C", "本案 Claim 1 技術要件": "該主樹脂基質與修飾奈米二氧化矽之特定重量比為 1:2.5 至 1:4.0，於 10 GHz 下介電損耗 Df 小於 0.003", "前案 D1 對應技術": "重量比未限定，常態為 1:1.5 以下", "前案 D2 對應技術": "教示填料高於 1:2 時黏度急遽飆升失去加工性 (Negative Teaching)", "符合性判定": "NO (不符/差異點)", "差異/進步性說明": "核心臨界配比：突破引證案 D2 之高填料黏度障礙，在特定 1:2.5~1:4.0 比例下兼具高導熱與超低 Df 協同增效。"},
                {"要件編號": "Element 1D", "本案 Claim 1 技術要件": "包含 15 至 35 重量份之活性酚類或酸酐類硬化劑及促進劑", "前案 D1 對應技術": "常規雙氰胺 (DICY) 硬化劑", "前案 D2 對應技術": "酸酐硬化劑", "符合性判定": "均等成立 (DOE)", "差異/進步性說明": "提供交聯固化基本功能，屬所屬技術領域具通常知識者所能置換之均等構件。"}
            ]
            st.session_state["last_oa_result"] = OA_CHEM_FORMULA_DOC

        elif selected_template == "貴金屬電鍍晶粒細化光澤劑 (化學配方)":
            st.session_state["patent_title_input"] = "用於貴金屬電鍍之晶粒細化光澤添加劑組成物"
            st.session_state["is_chemical_patent"] = True
            st.session_state["ipc_input_val"] = "C25D 3/46, C25D 3/48, C25D 3/62, C25D 3/64"
            st.session_state["cpc_input_val"] = "C25D 3/46, C25D 3/48, C25D 3/64"
            st.session_state["p1_n_val"] = "Target: 貴金屬電鍍浴與接觸件"
            st.session_state["p1_e_val"] = "electroplating bath, gold electroplating, silver plating, contact terminal"
            st.session_state["p1_z_val"] = "電鍍浴, 鍍金, 鍍銀, 接觸端子, 引線框架, 貴金屬沉積"
            st.session_state["p2_n_val"] = "Components: 雜環季銨鹽與含硫細化劑協同"
            st.session_state["p2_e_val"] = "grain refiner, brightener, quaternary ammonium, heterocyclic compound"
            st.session_state["p2_z_val"] = "晶粒細化劑, 光澤劑, 聚季銨鹽, 芳香雜環, 硫丙基二硫化物, 陰極極化"
            st.session_state["p3_n_val"] = "Property: 奈米微晶緻密與耐磨抗氧化"
            st.session_state["p3_e_val"] = "nanocrystalline, dendritic suppression, low contact resistance, wear resistance"
            st.session_state["p3_z_val"] = "奈米晶粒, 抑制枝晶, 低接觸阻抗, 耐磨耗, 打線結合力, 鏡面光澤"
            st.session_state["claims_data"] = [
                {"要件編號": "Element 1A", "本案 Claim 1 技術要件": "一貴金屬電鍍添加劑，包含 0.1~10 重量份之主光澤劑，其具含氮芳香雜環或聚季銨鹽陽離子結構", "前案 D1 對應技術": "常規吡啶衍生物單一有機光澤劑", "前案 D2 對應技術": "硫脲類晶粒抑制劑", "符合性判定": "NO (不符/差異點)", "差異/進步性說明": "本案特定聚季銨鹽結構具強陰極極化能力，不易高溫裂解。"},
                {"要件編號": "Element 1B", "本案 Claim 1 技術要件": "包含 0.05~5 重量份之輔助細化劑，選自含硫或磺酸基有機抑制劑（如 MPS/SPS 類）", "前案 D1 對應技術": "游離磺酸鹽載體", "前案 D2 對應技術": "含硫醇基界面整平劑", "符合性判定": "YES (字面讀取)", "差異/進步性說明": "公知含硫去極化構件。"},
                {"要件編號": "Element 1C", "本案 Claim 1 技術要件": "該主光澤劑與輔助細化劑之重量比限定為 1:1 至 10:1，具特定吸附平衡比例", "前案 D1 對應技術": "未限定特定重量配比，由操作者隨機添加", "前案 D2 對應技術": "比例為 1:20 之微量添加體系", "符合性判定": "NO (不符/差異點)", "差異/進步性說明": "特定 1:1~10:1 配比產生過電位負移 50~200 mV 協同增效，晶粒細化至 80 nm 且無脆化。"},
                {"要件編號": "Element 1D", "本案 Claim 1 技術要件": "包含 0.5~8 重量份之極化調節界面活性劑與溶劑載體", "前案 D1 對應技術": "PEG-400 界面活性劑", "前案 D2 對應技術": "陰離子活性劑", "符合性判定": "均等成立 (DOE)", "差異/進步性說明": "提供基本潤濕消泡功效，屬等效置換之均等構件。"}
            ]
            st.session_state["last_oa_result"] = OA_ELECTROPLATING_DOC

        elif selected_template == "多光譜溫室作物病害早期偵測系統":
            st.session_state["patent_title_input"] = "多光譜溫室作物病害早期偵測系統"
            st.session_state["is_chemical_patent"] = False
            st.session_state["ipc_input_val"] = "A01G 9/24, G01N 21/84, G06V 20/10"
            st.session_state["cpc_input_val"] = "A01G 9/24, G01N 2021/8466"
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
                {"要件編號": "Element 1A", "本案 Claim 1 技術要件": "一多光譜感測模組，配置於移動軌道，具有特定吸收峰窄波段濾波感測器", "前案 D1 對應技術": "常規 RGB 廣角監視器", "前案 D2 對應技術": "手持式分光輻射計", "符合性判定": "NO (不符/差異點)", "差異/進步性說明": "本案特定窄波段針對植物水分及葉綠素吸收峰。"},
                {"要件編號": "Element 1B", "本案 Claim 1 技術要件": "一邊緣推論處理器，對多光譜影像執行植被指數（NDVI/PRI）正規化降維校正", "前案 D1 對應技術": "影像壓縮後直接回傳伺服器", "前案 D2 對應技術": "離線電腦以 MATLAB 批次運算", "符合性判定": "NO (不符/差異點)", "差異/進步性說明": "本案於邊緣端完成即時反光補償與指數特徵化。"},
                {"要件編號": "Element 1C", "本案 Claim 1 技術要件": "一病斑早期預警神經網路模型，根據特徵化多光譜資訊預測前症狀潛伏病灶", "前案 D1 對應技術": "色差比對判定枯黃斑塊", "前案 D2 對應技術": "葉片病徵分類 CNN", "符合性判定": "NO (不符/差異點)", "差異/進步性說明": "葉片肉眼顯性病變前 48 小時預警。"}
            ]
        st.success(f"已載入範本：{selected_template}")
        st.rerun()

with col_tmpl2:
    if st.button("🧹 清空所有", use_container_width=True):
        st.session_state["patent_title_input"] = ""
        st.session_state["ipc_input_val"] = ""
        st.session_state["cpc_input_val"] = ""
        st.session_state["p1_n_val"] = "Target: 應用標的"
        st.session_state["p1_e_val"] = ""
        st.session_state["p1_z_val"] = ""
        st.session_state["p2_n_val"] = "Mechanism/Components: 核心手段/組分"
        st.session_state["p2_e_val"] = ""
        st.session_state["p2_z_val"] = ""
        st.session_state["p3_n_val"] = "Effect/Property: 技術功效/物化特性"
        st.session_state["p3_e_val"] = ""
        st.session_state["p3_z_val"] = ""
        st.session_state["is_chemical_patent"] = False
        st.session_state["claims_data"] = [
            {"要件編號": "Element 1A", "本案 Claim 1 技術要件": "", "前案 D1 對應技術": "", "前案 D2 對應技術": "", "符合性判定": "待確認", "差異/進步性說明": ""}
        ]
        st.session_state["last_oa_result"] = None
        st.session_state["last_fetched_patent"] = None
        st.info("已清空所有輸入欄位與矩陣。")
        st.rerun()

with st.sidebar.expander("📖 操作手冊與使用說明", expanded=False):
    st.markdown(USER_MANUAL_MARKDOWN)
    st.download_button(
        label="📥 下載操作手冊 (.md)",
        data=USER_MANUAL_MARKDOWN,
        file_name="IP_Workbench_User_Manual.md",
        mime="text/markdown",
        use_container_width=True
    )

tab_patent, tab_trade_secret, tab_trademark, tab_laws = st.tabs([
    "📄 專利檢索與 Claims 比對矩陣 (含化學配方)",
    "🔐 營業秘密 vs. 專利策略矩陣",
    "🏷️ 商標權佈局與圖樣生成器",
    "⚖️ 智財法規速查 (專利/商標/營業秘密)"
])

# ==============================================================================
# TAB 1: 專利權模組 (特化化學配方專利)
# ==============================================================================
with tab_patent:
    st.subheader("1. 發明標的名稱與 AI 自動拆解")
    col_input1, col_input2 = st.columns([3, 1])

    with col_input1:
        target_title = st.text_input(
            "請輸入專利標的名稱：",
            key="patent_title_input",
            placeholder="例如：半導體先進封裝用低介電高散熱環氧樹脂填料組成物 或 晶圓搬運機械手臂"
        )
        is_chem = st.checkbox("🧪 本案為化學/配方/材料組成物發明 (啟動組分配比與協同增效特化拆解)", key="is_chemical_patent")

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
            with st.spinner("🤖 正在調用 Gemini 拆解技術特徵（含化學配方特化邏輯）..."):
                try:
                    ai_res = analyze_patent_with_gemini(user_api_key.strip(), target_title.strip(), is_chemical=is_chem)

                    st.session_state["ipc_input_val"] = ai_res.get("ipc", "")
                    st.session_state["cpc_input_val"] = ai_res.get("cpc", "")
                    st.session_state["p1_n_val"] = ai_res.get("pillar_a_name", "Target: 應用標的")
                    st.session_state["p1_e_val"] = ai_res.get("pillar_a_en", "")
                    st.session_state["p1_z_val"] = ai_res.get("pillar_a_zh", "")
                    st.session_state["p2_n_val"] = ai_res.get("pillar_b_name", "Mechanism/Components: 核心手段/組分")
                    st.session_state["p2_e_val"] = ai_res.get("pillar_b_en", "")
                    st.session_state["p2_z_val"] = ai_res.get("pillar_b_zh", "")
                    st.session_state["p3_n_val"] = ai_res.get("pillar_c_name", "Effect/Property: 技術功效/物化特性")
                    st.session_state["p3_e_val"] = ai_res.get("pillar_c_en", "")
                    st.session_state["p3_z_val"] = ai_res.get("pillar_c_zh", "")
                    
                    if ai_res.get("claim_elements"):
                        st.session_state["claims_data"] = ai_res.get("claim_elements")

                    st.success("🎉 Gemini AI 拆解完成！三支柱欄位與 Claims 已同步填入！")
                    st.rerun()
                except Exception as e:
                    st.error(f"AI 呼叫失敗，請稍後重試。詳細原因: {e}")

    col_class1, col_class2 = st.columns(2)
    with col_class1:
        ipc_input = st.text_input("IPC 分類號 (逗號隔開)", key="ipc_input_val", placeholder="例: C08L 63/00, C25D 3/46")
    with col_class2:
        cpc_input = st.text_input("CPC 分類號 (逗號隔開)", key="cpc_input_val", placeholder="例: C08L 63/00, C25D 3/64")

    st.markdown("---")
    st.subheader("2. 技術特徵三支柱展開")

    col_p1, col_p2, col_p3 = st.columns(3)
    with col_p1:
        st.markdown("#### 支柱 A：應用標的 / 母材 (Target)")
        p1_name = st.text_input("支柱 A 名稱", key="p1_n_val")
        p1_en = st.text_area("英文關鍵字 (逗號隔開)", key="p1_e_val", height=100)
        p1_zh = st.text_area("中文關鍵字 (逗號隔開)", key="p1_z_val", height=100)

    with col_p2:
        st.markdown("#### 支柱 B：核心組分 / 手段 (Components)")
        p2_name = st.text_input("支柱 B 名稱", key="p2_n_val")
        p2_en = st.text_area("英文關鍵字 (逗號隔開)", key="p2_e_val", height=100)
        p2_zh = st.text_area("中文關鍵字 (逗號隔開)", key="p2_z_val", height=100)

    with col_p3:
        st.markdown("#### 支柱 C：技術功效 / 物性 (Property)")
        p3_name = st.text_input("支柱 C 名稱", key="p3_n_val")
        p3_en = st.text_area("英文關鍵字 (逗號隔開)", key="p3_e_val", height=100)
        p3_zh = st.text_area("中文關鍵字 (逗號隔開)", key="p3_z_val", height=100)

    st.markdown("---")
    st.subheader("3. 引證前案專利號爬取與自動比對 (Auto-fetch Prior Art)")
    col_fetch1, col_fetch2, col_fetch3 = st.columns([2, 1, 1])
    with col_fetch1:
        target_pno = st.text_input("前案專利號 (公開號/公告號)：", placeholder="例如：US11578418B2 或 US6165342A", key="fetch_pno_input")
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
                    if not curr_claims:
                        curr_claims = [
                            {"要件編號": "Element 1A", "本案 Claim 1 技術要件": "主樹脂/主要構件", "前案 D1 對應技術": "", "前案 D2 對應技術": "", "符合性判定": "待確認", "差異/進步性說明": ""},
                            {"要件編號": "Element 1B", "本案 Claim 1 技術要件": "改質填料/特徵手段", "前案 D1 對應技術": "", "前案 D2 對應技術": "", "符合性判定": "待確認", "差異/進步性說明": ""},
                            {"要件編號": "Element 1C", "本案 Claim 1 技術要件": "特定配比/臨界數值", "前案 D1 對應技術": "", "前案 D2 對應技術": "", "符合性判定": "待確認", "差異/進步性說明": ""}
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
                    st.success(f"✅ 成功擷取專利：【{p_data['patent_no']}】{p_data['title']}，已完成比對！")
                    st.rerun()

                except Exception as e:
                    st.error(f"爬取或比對失敗: {e}")

    if st.session_state["last_fetched_patent"]:
        last_p = st.session_state["last_fetched_patent"]
        with st.expander(f"📖 查看最近爬取之專利原文：【{last_p['patent_no']}】{last_p['title']}", expanded=False):
            st.markdown(f"**專利名稱**：{last_p['title']}")
            st.markdown(f"**專利號**：`{last_p['patent_no']}` ｜ [在 Google Patents 開啟原文]({last_p['url']})")
            st.markdown("##### 📄 專利說明書摘要")
            st.info(last_p["abstract"] if last_p["abstract"] else "無摘要內容")
            st.markdown("##### ⚖ 申請專利範圍原文 (Claims)")
            st.code(last_p["claims"] if last_p["claims"] else "無 Claims 內容", language="text")

    st.markdown("---")
    st.subheader("4. 申請專利範圍全要件比對矩陣 (線上編輯)")
    current_claims = st.session_state["claims_data"]
    
    edited_df = st.data_editor(
        pd.DataFrame(current_claims),
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
        key="claim_editor_live"
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
            with st.spinner("🤖 正在提煉差異點與配方協同增效，撰寫專利法第22條進步性理由書..."):
                diff_rows = []
                for _, r in edited_df.iterrows():
                    elem = r.get("要件編號", "")
                    claim_desc = r.get("本案 Claim 1 技術要件", "")
                    d1_desc = r.get("前案 D1 對應技術", "")
                    diff_note = r.get("差異/進步性說明", "")
                    diff_rows.append(f"【{elem}】本案要件：{claim_desc}；前案技術：{d1_desc}；差異與進步功效：{diff_note}")

                diff_summary = "\n".join(diff_rows)
                rejection_summary = "審查官認為本案 Claim 1 組成物各組分已被先前技術個別教示，所限定之成分重量比為通常知識者之常規試誤調整，欠缺進步性。"
                
                try:
                    quick_oa_res = generate_oa_response_with_gemini(
                        user_api_key.strip(),
                        "專利法第 22 條第 2 項（進步性核駁 - 含化學配方數值臨界性與突變增效抗辯）",
                        target_title if target_title else "本發明專利申請案",
                        rejection_summary,
                        diff_summary
                    )
                    st.session_state["last_oa_result"] = quick_oa_res
                    st.success("🎉 進步性申復理由書產生完成！")
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
    col_opt1, col_opt2 = st.columns(2)
    with col_opt1:
        include_effect = st.checkbox("🔍 Google 檢索式納入技術功效詞（Pillar C）", value=False, help="預設取消勾選以防止過度限縮檢索結果而掛零；若前案過多再勾選此項。")

    if st.button("🚀 生成專利檢索式並整合比對報告", type="primary", use_container_width=True):
        builder = PatentSearchBuilder(target_title if target_title else "未命名技術標的")
        if ipc_input:
            builder.add_ipc(ipc_input)
        if cpc_input:
            builder.add_cpc(cpc_input)

        builder.add_pillar(p1_name, p1_en.split(",") if p1_en else [], p1_zh.split(",") if p1_zh else [])
        builder.add_pillar(p2_name, p2_en.split(",") if p2_en else [], p2_zh.split(",") if p2_zh else [])
        builder.add_pillar(p3_name, p3_en.split(",") if p3_en else [], p3_zh.split(",") if p3_zh else [])

        google_query = builder.to_google_patents_query(include_effect_pillar=include_effect)
        gpss_query = builder.to_gpss_query(include_effect_pillar=True)
        
        report_text = builder.generate_report_text(
            claim_chart_df=edited_df,
            prior_art_data=st.session_state.get("last_fetched_patent")
        )

        st.subheader("📋 產出結果")
        col_res1, col_res2 = st.columns(2)
        with col_res1:
            st.markdown("#### 🌐 Google Patents / Espacenet 檢索式 (洗淨防呆格式)")
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
    st.markdown("""
    在化學配方、材料製程或演算法領域，**「該申請專利公開，還是封存為營業秘密保護？」** 是企業最關鍵的智財決策。
    透過下方 5 大維度的量化權重指標，系統將自動為您運算並給出最佳保護策略建議。
    """)

    col_ts_eval1, col_ts_eval2 = st.columns(2)

    with col_ts_eval1:
        st.markdown("#### 1. 技術特性與外部逆向工程難易度")
        score_re = st.slider(
            "反向工程（Reverse Engineering）難度：",
            min_value=1, max_value=5, value=3,
            help="1分：對手買樣品化驗分析（如 GC-MS、NMR、SEM）即可輕易逆向解析出精準配方；5分：多重交聯反應、燒結混合或微量摻雜，化學逆向工程幾乎不可能還原。"
        )
        
        score_detect = st.slider(
            "市場侵權可偵測性（Detectability of Infringement）：",
            min_value=1, max_value=5, value=2,
            help="1分：即便對手侵權使用你的配方/製程，從其市售成品表面完全看不出來且無法採證；5分：只要分析對手市售商品即可直接取得侵權之特徵證據。"
        )

        score_lifecycle = st.slider(
            "產品/技術市場生命週期（Market Life Cycle）：",
            min_value=1, max_value=5, value=4,
            help="1分：消費電子快速更迭（1~2年淘汰）；5分：長青型基礎材料/經典配方（預期市場價值超過 20 年專利保護期）。"
        )

    with col_ts_eval2:
        st.markdown("#### 2. 製程特性與企業內部管控力")
        score_process = st.slider(
            "技術核心偏向製程操作 vs. 終端成品：",
            min_value=1, max_value=5, value=4,
            help="1分：技術特徵純為市售終端產品之成分；5分：技術特徵高度依賴內部密閉反應釜溫度/壓力/滴加速度等內部黑箱製程參數。"
        )

        score_protection = st.slider(
            "企業內部合理保密措施完備程度：",
            min_value=1, max_value=5, value=3,
            help="1分：無 NDA、無門禁分流、人員流動大；5分：配方拆解代工、核心機密分段隔離、已建立 ISO 27001 與營業秘密資安稽核制度。"
        )

    ts_weighted_score = (score_re * 0.25) + ((6 - score_detect) * 0.25) + (score_lifecycle * 0.15) + (score_process * 0.20) + (score_protection * 0.15)

    st.markdown("---")
    st.subheader("📊 策略決策矩陣運算結果")

    col_res_ts1, col_res_ts2 = st.columns([1, 2])
    with col_res_ts1:
        st.metric(label="營業秘密傾向指數 (Trade Secret Index)", value=f"{ts_weighted_score:.2f} / 5.0")
        if ts_weighted_score >= 3.6:
            st.success("🎯 **強烈建議：封存為【營業秘密】保護**")
        elif ts_weighted_score >= 2.8:
            st.warning("⚖️ **雙軌佈局：【專利 ＋ 營業秘密】混合防禦**")
        else:
            st.info("📄 **強烈建議：全面申請【發明專利】公開排他**")

    with col_res_ts2:
        if ts_weighted_score >= 3.6:
            st.markdown("""
            **【策略理由與建議行動】**：
            1. **反向工程門檻極高**，且外部市售品難以直接採證侵權，若公開專利反而是向全世界競爭對手「免費公開技術核心教示」。
            2. **建議作為**：立即依《營業秘密法》第 2 條建立**「合理保密措施」**：
               - 將配方組份進行代號化管理（例如 Ingredient A-102），由不同廠區分段投料。
               - 研發人員簽署嚴謹之離職競業禁止與營業秘密保護約定書。
               - 機密文件與配方表設定浮水印及內部伺服器讀取稽核記錄。
            """)
        elif ts_weighted_score >= 2.8:
            st.markdown("""
            **【策略理由與建議行動】**：
            1. 建議採取**「專利護城河 ＋ 營業秘密黑箱」的混合雙軌策略**。
            2. **專利保護部分**：針對終端產物之「主要化學組分與寬廣的配比範圍」申請專利，以公開排他權阻止對手大舉進犯。
            3. **營業秘密保護部分**：將「最佳黃金比例（Sweet Spot）、反應催化劑具體添加溫度、精確攪拌剪切速率等 Know-how」保留為內部營業秘密，不於專利說明書中全部揭露。
            """)
        else:
            st.markdown("""
            **【策略理由與建議行動】**：
            1. **外部逆向工程容易，或市售品極易化驗比對**。一旦他人購得產品即可透過分析破解；營業秘密無法防禦善意第三人之反向工程！
            2. 產品若被對手搶先申請專利，我方反而可能面臨專利侵權指控。
            3. **建議作為**：儘速撰寫專利說明書，佈局「組成物請求項」與「用途請求項」，鎖定國內外主要市場申請發明專利！
            """)

    st.markdown("---")
    st.subheader("📋 《營業秘密法》第 2 條法定三要件實體檢核清單")
    st.caption("企業若欲在訴訟中勝訴，法官將嚴格審查以下 3 項法定要件是否具備：")

    col_chk1, col_chk2, col_chk3 = st.columns(3)
    with col_chk1:
        st.markdown("##### 1. 秘密性 (Secrecy)")
        st.checkbox("非一般涉及該類資訊之人所知悉", value=True)
        st.checkbox("配方或製程並未見於公開刊物、論文或既有專利中", value=True)
        st.checkbox("無法從市售成品透過常規反向工程直接窺知", value=True)
    with col_chk2:
        st.markdown("##### 2. 經濟價值性 (Economic Value)")
        st.checkbox("能為企業帶來實質生產效率或成本優勢", value=True)
        st.checkbox("若被競爭對手取得將造成市場份額流失", value=True)
        st.checkbox("投入研發資金、反覆試錯之實質研發成本", value=True)
    with col_chk3:
        st.markdown("##### 3. 合理保密措施 (Reasonable Measures)")
        st.checkbox("配方資料載明「機密 (Confidential)」標識", value=False)
        st.checkbox("涉密人員簽署保密協定（NDA）及離職約定", value=True)
        st.checkbox("配方資訊分級授權，非涉密人員無法調閱", value=False)
        st.checkbox("實施廠區分段投料或代號管理，避免全貌外洩", value=False)

# ==============================================================================
# TAB 3: 商標權模組
# ==============================================================================
with tab_trademark:
    st.subheader("🏷️ 商標尼斯分類佈局與 TIPO 規範圖樣產生器")
    st.markdown("評估商標識別性（Distinctiveness）、自動推薦化學原料（第01類）或高科技軟硬體分類，並支援合成符合智財局規範之申請圖檔。")

    col_tm1, col_tm2 = st.columns([1, 1])

    with col_tm1:
        st.markdown("#### 1. 品牌標的與產品資訊")
        tm_brand = st.text_input("擬申請商標文字 (中/英文)：", value="極塑 PolyUltra", placeholder="例如：極塑 PolyUltra 或 葉語 SpectrIQ")
        tm_desc = st.text_area("產品或服務技術概述：", value="用於先進半導體覆晶封裝與晶圓級封裝之低介電環氧樹脂填料化合物、工業用化學複合材料。", height=100)

        if st.button("✨ 執行商標識別性與尼斯分類 AI 評估", type="secondary", use_container_width=True, key="btn_tm_ai"):
            if not user_api_key.strip():
                st.error("請先於左側側邊欄輸入有效的 Gemini API Key！")
            elif not tm_brand.strip():
                st.warning("請填寫擬申請之商標文字。")
            else:
                with st.spinner("🤖 正在調用 Gemini 評估商標識別性與分類..."):
                    try:
                        st.session_state["tm_analysis"] = analyze_trademark_with_gemini(user_api_key.strip(), tm_brand.strip(), tm_desc.strip())
                        st.success("🎉 商標分析完成！")
                    except Exception as e:
                        st.error(f"分析失敗: {e}")

        if st.session_state.get("tm_analysis"):
            res = st.session_state["tm_analysis"]
            st.markdown("---")
            st.markdown("#### 📋 智財審查可行性分析")
            st.info(f"**識別性等級判定**：{res.get('distinctiveness_level', '未知')}\n
