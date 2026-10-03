import streamlit as st
from dataclasses import dataclass, field
from typing import List, Dict

# ==============================================================================
# 一、 核心資料結構與邏輯
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

# ==============================================================================
# 二、 Streamlit 介面配置
# ==============================================================================
st.set_page_config(
    page_title="專利檢索字串生成器",
    page_icon="🔍",
    layout="wide"
)

st.title("🔍 專業專利檢索邏輯式生成工具")
st.markdown("快速將技術特徵三支柱與分類號轉換為 **Google Patents** 與 **台灣智慧局 GPSS** 檢索語法。")

# 預設模板選擇
st.sidebar.header("📁 載入範例模板")
template = st.sidebar.selectbox(
    "選擇預設技術標的快速測試：",
    ["空白自訂", "邊緣運算光學瑕疵檢測", "無鏈條齒輪箱無段變速花轂"]
)

# 依模板給定預設值
default_title = ""
default_ipc = ""
default_cpc = ""
default_p1_name = "支柱 A: 應用標的"
default_p1_en = ""
default_p1_zh = ""
default_p2_name = "支柱 B: 核心手段/機構"
default_p2_en = ""
default_p2_zh = ""
default_p3_name = "支柱 C: 技術功效/特徵"
default_p3_en = ""
default_p3_zh = ""

if template == "邊緣運算光學瑕疵檢測":
    default_title = "基於邊緣運算之即時影像瑕疵檢測系統"
    default_ipc = "G06T 7/00, G01N 21/88"
    default_cpc = "G06V 10/00"
    default_p1_name = "Target: 瑕疵檢測"
    default_p1_en = "defect detection, flaw inspection, surface anomaly"
    default_p1_zh = "瑕疵檢測, 缺陷檢驗, 表面異常"
    default_p2_name = "Mechanism: 邊緣運算與視覺推論"
    default_p2_en = "edge computing, neural network, real-time inferenc*"
    default_p2_zh = "邊緣運算, 神經網絡, 即時推論, 深度學習"
    default_p3_name = "Effect: 低延遲與高精度"
    default_p3_en = "low latency, high throughput, false positive reduction"
    default_p3_zh = "低延遲, 降低誤判, 即時處理"

elif template == "無鏈條齒輪箱無段變速花轂":
    default_title = "無鏈條傳動之齒輪箱無段變速自行車花轂"
    default_ipc = "B62M 17/00, B62M 11/16, F16H 15/52"
    default_cpc = ""
    default_p1_name = "Target: 自行車與花轂"
    default_p1_en = "bicycle, bike, bicycle hub, wheel hub"
    default_p1_zh = "自行車, 腳踏車, 車轂, 花轂, 輪轂"
    default_p2_name = "Mechanism: 無鏈條傳動與齒輪箱"
    default_p2_en = "chainless, shaft drive, transmission shaft, gearbox, bevel gear"
    default_p2_zh = "無鏈, 軸傳動, 傳動軸, 齒輪箱, 傘齒輪"
    default_p3_name = "Mechanism: 無段變速 (CVT)"
    default_p3_en = "continuously variable, CVT, infinitely variable, friction drive, traction drive"
    default_p3_zh = "無段變速, 無級變速, 摩擦傳動, 球體傳動"

# 表單輸入
with st.container():
    st.subheader("1. 發明基本資料與分類號")
    col1, col2, col3 = st.columns([2, 1, 1])
    with col1:
        target_title = st.text_input("專利標的名稱", value=default_title, placeholder="例如：自動化植物工廠環境控制系統")
    with col2:
        ipc_input = st.text_input("IPC 分類號 (逗號隔開)", value=default_ipc, placeholder="例: B62M 17/00, F16H 15/52")
    with col3:
        cpc_input = st.text_input("CPC 分類號 (逗號隔開)", value=default_cpc, placeholder="例: G06V 10/00")

st.markdown("---")
st.subheader("2. 技術三支柱特徵拆解")

col_p1, col_p2, col_p3 = st.columns(3)

with col_p1:
    st.markdown("#### 支柱 A：應用標的 (Target)")
    p1_name = st.text_input("支柱 A 名稱", value=default_p1_name, key="p1_n")
    p1_en = st.text_area("英文關鍵字 (逗號隔開)", value=default_p1_en, key="p1_e", height=100)
    p1_zh = st.text_area("中文關鍵字 (逗號隔開)", value=default_p1_zh, key="p1_z", height=100)

with col_p2:
    st.markdown("#### 支柱 B：核心手段 (Mechanism)")
    p2_name = st.text_input("支柱 B 名稱", value=default_p2_name, key="p2_n")
    p2_en = st.text_area("英文關鍵字 (逗號隔開)", value=default_p2_en, key="p2_e", height=100)
    p2_zh = st.text_area("中文關鍵字 (逗號隔開)", value=default_p2_zh, key="p2_z", height=100)

with col_p3:
    st.markdown("#### 支柱 C：技術功效 (Effect)")
    p3_name = st.text_input("支柱 C 名稱", value=default_p3_name, key="p3_n")
    p3_en = st.text_area("英文關鍵字 (逗號隔開)", value=default_p3_en, key="p3_e", height=100)
    p3_zh = st.text_area("中文關鍵字 (逗號隔開)", value=default_p3_zh, key="p3_z", height=100)

st.markdown("---")

# 生成與產出
if st.button("🚀 生成各國專利檢索邏輯式", type="primary", use_container_width=True):
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

    st.subheader("📋 產出結果")
    
    col_res1, col_res2 = st.columns(2)
    
    with col_res1:
        st.markdown("#### 🌐 Google Patents / Espacenet 檢索式")
        st.text_area("複製以下字串貼至 Google Patents 搜尋框：", value=google_query, height=150)
        st.caption("規則：英文同義詞 OR 連接、多詞自動引號、跨支柱 AND 組合，並加上分類號條件。")
        
    with col_res2:
        st.markdown("#### 🇹🇼 台灣智慧局 GPSS 檢索式")
        st.text_area("複製以下字串貼至 GPSS 布林/進階檢索框：", value=gpss_query, height=150)
        st.caption("規則：限定名稱/摘要/專利範圍 TI,AB,CL，中英同義詞合併，附加 IC 國際分類碼。")
