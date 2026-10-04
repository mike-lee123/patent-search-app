import streamlit as st
import streamlit.components.v1 as components
import pandas as pd
import json
import os
import io
import urllib.parse
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
# 一、 核心資料結構與邏輯 (專利端)
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

    def generate_report_text(self, claim_chart_df: pd.DataFrame = None) -> str:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        lines = [
            "=" * 85,
            f"專利檢索與技術特徵分析報告 (含 Claims 檢核表與線上編輯比對矩陣)",
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
        return "\n".join(lines)

# ==============================================================================
# 二、 商標圖樣繪製核心邏輯 (符合 TIPO 規範)
# ==============================================================================
def create_tipo_trademark_bytes(text: str, layout: str = "單行水平置中", font_size: int = 76) -> bytes:
    """產生符合 TIPO 電子送件 8x8 cm 300DPI 規格之 JPEG bytes"""
    dpi = 300
    cm_to_inch = 2.54
    width_px = int((8.0 / cm_to_inch) * dpi)   # 944~945 px
    height_px = int((8.0 / cm_to_inch) * dpi)

    image = Image.new("RGB", (width_px, height_px), color=(255, 255, 255))
    draw = ImageDraw.Draw(image)

    # 字型自動偵測
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

    if layout == "上下雙行置中":
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
        # 單行水平置中
        bbox = draw.textbbox((0, 0), text, font=font)
        w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
        x = (width_px - w) / 2 - bbox[0]
        y = (height_px - h) / 2 - bbox[1]
        draw.text((x, y), text, font=font, fill=(0, 0, 0))

    img_buffer = io.BytesIO()
    image.save(img_buffer, format="JPEG", dpi=(dpi, dpi), quality=95, subsampling=0)
    return img_buffer.getvalue()

# ==============================================================================
# 三、 Gemini AI 自動分析輔助函式
# ==============================================================================
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
                "前案 D1 對應技術": "常見習知技術或空白",
                "前案 D2 對應技術": "常見習知技術或空白",
                "符合性判定": "待確認",
                "差異/進步性說明": "預估發明點或技術功效說明"
            }}
        ]
    }}
    """
    response = client.models.generate_content(
        model='gemini-2.5-flash',
        contents=prompt,
        config=types.GenerateContentConfig(response_mime_type="application/json")
    )
    return json.loads(response.text)

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
    response = client.models.generate_content(
        model='gemini-2.5-flash',
        contents=prompt,
        config=types.GenerateContentConfig(response_mime_type="application/json")
    )
    return json.loads(response.text)

# ==============================================================================
# 四、 複製輔助元件 (Clipboard)
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
# 五、 Streamlit 介面配置
# ==============================================================================
st.set_page_config(
    page_title="智慧財產權整合工作台 (專利檢索 ＆ 商標佈局)",
    page_icon="🛡️",
    layout="wide"
)

st.title("🛡️ 智慧財產權整合工作台 (專利 ＆ 商標)")
st.markdown("結合 **Google Patents 邏輯檢索**、**Claims 全要件比對矩陣**、**商標尼斯分類智慧佈局** 與 **TIPO 規範圖樣生成**。")

# --- 側邊欄設定 ---
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

# 頂部導覽分頁
tab_patent, tab_trademark = st.tabs(["📄 專利檢索與 Claims 比對矩陣", "🏷️ 商標權佈局與圖樣生成器"])

# ==============================================================================
# TAB 1: 專利權模組 (原有完整功能)
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
            with st.spinner("🤖 Gemini 正在分析技術特徵、比對 IPC/CPC 並擴展三支柱關鍵字..."):
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
                    st.success("🎉 Gemini AI 拆解完成！")
                    st.rerun()
                except Exception as e:
                    st.error(f"AI 呼叫失敗: {e}")

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
    st.subheader("3. 申請專利範圍全要件比對矩陣 (線上編輯)")
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
            "前案 D1 對應技術": st.column_config.TextColumn("前案 D1 [專利號:________]", width="medium"),
            "前案 D2 對應技術": st.column_config.TextColumn("前案 D2 [專利號:________]", width="medium"),
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
        report_text = builder.generate_report_text(claim_chart_df=edited_df)

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
            st.download_button("📥 下載完整檢索分析報告 (.txt)", data=report_text, file_name=f"patent_analysis_{time_str}.txt", mime="text/plain", type="primary", use_container_width=True)
        with col_dl2:
            st.download_button("📊 下載前案比對矩陣 (.csv)", data=csv_bytes, file_name=f"claim_chart_{time_str}.csv", mime="text/csv", type="secondary", use_container_width=True)

# ==============================================================================
# TAB 2: 商標權模組 (全新整合)
# ==============================================================================
with tab_trademark:
    st.subheader("🏷️ 商標尼斯分類佈局與 TIPO 規範圖樣產生器")
    st.markdown("針對品牌名稱評估識別性（Distinctiveness）、自動推薦第 09/42 等尼斯分類商品，並直接產出符合智財局規格的白底黑字標準申請圖檔。")

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
                with st.spinner("🤖 Gemini 正在評估商標識別性與匹配尼斯分類..."):
                    try:
                        st.session_state.tm_analysis = analyze_trademark_with_gemini(user_api_key.strip(), tm_brand.strip(), tm_desc.strip())
                        st.success("🎉 商標分析完成！")
                    except Exception as e:
                        st.error(f"分析失敗: {e}")

        # AI 分析結果呈現
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
            st.link_button("🇹🇼 開啟經濟部智慧局商標檢索系統", "https://tmsearch.tipo.gov.tw/", use_container_width=True)

    with col_tm2:
        st.markdown("#### 2. TIPO 電子送件商標圖樣即時產生器")
        st.caption("符合標準：8×8 公分、300 DPI、945×945 px、純白底色、墨色黑色、RGB 模式 JPEG。")

        layout_choice = st.radio("圖樣排版方式：", ["單行水平置中", "上下雙行置中"], horizontal=True)
        font_size_val = st.slider("字級大小 (Font Size)：", min_value=40, max_value=120, value=76, step=2)

        # 動態繪製圖樣
        if tm_brand.strip():
            img_bytes = create_tipo_trademark_bytes(tm_brand.strip(), layout=layout_choice, font_size=font_size_val)
            st.image(img_bytes, caption="📸 圖樣預覽 (8x8 cm @ 300 DPI 標準白底黑字)", width=320)

            clean_filename = f"trademark_{tm_brand.strip().replace(' ', '_')}.jpg"
            st.download_button(
                label="📥 下載標準商標圖樣檔 (.jpg)",
                data=img_bytes,
                file_name=clean_filename,
                mime="image/jpeg",
                type="primary",
                use_container_width=True
            )
            st.caption("💡 說明：此 JPG 圖檔可直接於智慧局 E-filing 電子送件系統中作為正式商標圖樣上傳，無須另外後製轉檔。")
        else:
            st.warning("請先於左側輸入商標名稱以生成圖樣。")
