import streamlit as st
import pandas as pd
from datetime import datetime
from typing import List, Dict, Optional, Any


def _normalize_name(raw_name: str) -> str:
    """İsimdeki fazla boşlukları temizler ve standartlaştırır."""
    if not raw_name:
        return ""
    return " ".join(str(raw_name).strip().split())


def _turkish_sort_key(text: str) -> str:
    """Türkçe karakter duyarlı alfabetik sıralama anahtarı."""
    tr_map = str.maketrans(
        "çğıöşüÇĞİIÖŞÜ",
        "cgiosuCGIIOSU"
    )
    return text.translate(tr_map).lower()


def get_known_members(
    debts: List[Dict[str, Any]],
    transactions: Optional[List[Dict[str, Any]]] = None
) -> List[str]:
    """
    Borç (ve opsiyonel olarak işlem) kayıtlarındaki mevcut sporcu / kişi / kurum
    isimlerini tekrarsız ve alfabetik sıralı olarak döndürür.

    Args:
        debts: Borç kayıtları listesi.
        transactions: Opsiyonel işlem kayıtları listesi.

    Returns:
        Alfabetik sıralı, tekrarsız kişi/kurum isimleri listesi.
    """
    seen_lower: Dict[str, str] = {}

    if debts:
        for d in debts:
            raw = d.get("name")
            clean = _normalize_name(raw)
            if clean:
                key = clean.lower()
                if key not in seen_lower:
                    seen_lower[key] = clean

    if transactions:
        for tx in transactions:
            desc = _normalize_name(tx.get("description") or "")
            # "(Borç Kapatma)" ile biten otomatik işlem açıklamalarından kişi adını ayıkla
            if desc.endswith("(Borç Kapatma)"):
                candidate = _normalize_name(desc.replace("(Borç Kapatma)", ""))
                if candidate:
                    key = candidate.lower()
                    if key not in seen_lower:
                        seen_lower[key] = candidate

    members = list(seen_lower.values())
    members.sort(key=_turkish_sort_key)
    return members


def _get_due_status_info(due_date_str: Optional[str]) -> Dict[str, Any]:
    """Vade tarihini analiz ederek gün farkını, durumu ve rozet HTML'ini döndürür."""
    if not due_date_str:
        return {
            "days_diff": None,
            "is_overdue": False,
            "is_due_soon": False,
            "label": "Vade Yok",
            "badge_html": (
                "<span style='background: rgba(100,116,139,0.1); color: #64748B; "
                "padding: 2px 8px; border-radius: 6px; font-size: 0.72rem; font-weight: 600;'>"
                "📅 Vadesiz</span>"
            ),
        }
    try:
        due = datetime.strptime(str(due_date_str), "%Y-%m-%d").date()
        today = datetime.now().date()
        diff = (due - today).days

        if diff < 0:
            return {
                "days_diff": diff,
                "is_overdue": True,
                "is_due_soon": False,
                "label": f"{abs(diff)} gün gecikti",
                "badge_html": (
                    f"<span style='background: rgba(239,68,68,0.14); color: #EF4444; "
                    f"border: 1px solid rgba(239,68,68,0.3); padding: 2px 8px; "
                    f"border-radius: 6px; font-size: 0.72rem; font-weight: 700;'>"
                    f"🔴 {abs(diff)} gün gecikti</span>"
                ),
            }
        elif diff == 0:
            return {
                "days_diff": 0,
                "is_overdue": True,
                "is_due_soon": True,
                "label": "Bugün son gün!",
                "badge_html": (
                    "<span style='background: rgba(239,68,68,0.14); color: #EF4444; "
                    "border: 1px solid rgba(239,68,68,0.3); padding: 2px 8px; "
                    "border-radius: 6px; font-size: 0.72rem; font-weight: 700;'>"
                    "🚨 Bugün son gün!</span>"
                ),
            }
        elif diff <= 7:
            return {
                "days_diff": diff,
                "is_overdue": False,
                "is_due_soon": True,
                "label": f"{diff} gün kaldı",
                "badge_html": (
                    f"<span style='background: rgba(217,119,6,0.12); color: #D97706; "
                    f"border: 1px solid rgba(217,119,6,0.25); padding: 2px 8px; "
                    f"border-radius: 6px; font-size: 0.72rem; font-weight: 600;'>"
                    f"⏳ {diff} gün kaldı</span>"
                ),
            }
        else:
            return {
                "days_diff": diff,
                "is_overdue": False,
                "is_due_soon": False,
                "label": f"{diff} gün kaldı",
                "badge_html": (
                    f"<span style='background: rgba(100,116,139,0.1); color: #64748B; "
                    f"border: 1px solid rgba(100,116,139,0.2); padding: 2px 8px; "
                    f"border-radius: 6px; font-size: 0.72rem; font-weight: 600;'>"
                    f"📅 {diff} gün kaldı</span>"
                ),
            }
    except Exception:
        return {
            "days_diff": None,
            "is_overdue": False,
            "is_due_soon": False,
            "label": str(due_date_str),
            "badge_html": "",
        }


def compute_member_summaries(debts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Borç kayıtlarını kişi/kurum bazında gruplayarak cari özet istatistiklerini hesaplar.
    """
    grouped: Dict[str, Dict[str, Any]] = {}

    for d in debts or []:
        raw_name = _normalize_name(d.get("name") or "İsimsiz")
        if not raw_name:
            raw_name = "İsimsiz"
        key = raw_name.lower()

        if key not in grouped:
            grouped[key] = {
                "name": raw_name,
                "pending_receivable": 0.0,  # Bekleyen Alınacak (Takımın alacağı)
                "pending_payable": 0.0,     # Bekleyen Verilecek (Takımın borcu)
                "paid_receivable": 0.0,
                "paid_payable": 0.0,
                "pending_count": 0,
                "paid_count": 0,
                "overdue_count": 0,
                "nearest_due_date": None,
                "records": [],
            }

        entry = grouped[key]
        amount = float(d.get("amount") or 0.0)
        d_type = d.get("type", "Alınacak")
        status = d.get("status", "Bekliyor")
        due_date = d.get("due_date")

        due_info = _get_due_status_info(due_date) if status == "Bekliyor" else {
            "days_diff": None,
            "is_overdue": False,
            "is_due_soon": False,
            "label": "Ödendi",
            "badge_html": (
                "<span style='background: rgba(16,185,129,0.12); color: #10B981; "
                "padding: 2px 8px; border-radius: 6px; font-size: 0.72rem; font-weight: 600;'>"
                "✅ Ödendi</span>"
            ),
        }

        enriched_record = {
            **d,
            "due_info": due_info,
        }
        entry["records"].append(enriched_record)

        if status == "Bekliyor":
            entry["pending_count"] += 1
            if d_type == "Alınacak":
                entry["pending_receivable"] += amount
            else:
                entry["pending_payable"] += amount

            if due_info["is_overdue"]:
                entry["overdue_count"] += 1

            if due_date:
                if entry["nearest_due_date"] is None or str(due_date) < str(entry["nearest_due_date"]):
                    entry["nearest_due_date"] = str(due_date)
        else:
            entry["paid_count"] += 1
            if d_type == "Alınacak":
                entry["paid_receivable"] += amount
            else:
                entry["paid_payable"] += amount

    summaries = []
    for entry in grouped.values():
        net_balance = round(entry["pending_receivable"] - entry["pending_payable"], 2)
        entry["pending_receivable"] = round(entry["pending_receivable"], 2)
        entry["pending_payable"] = round(entry["pending_payable"], 2)
        entry["net_balance"] = net_balance
        entry["total_records"] = entry["pending_count"] + entry["paid_count"]
        summaries.append(entry)

    # Varsayılan sıralama: Önce gecikmiş borcu olanlar, sonra mutlak net bakiyesi yüksek olanlar
    summaries.sort(
        key=lambda x: (
            x["pending_count"] == 0,
            -x["overdue_count"],
            -abs(x["net_balance"]),
            _turkish_sort_key(x["name"]),
        )
    )
    return summaries


def render_member_ledger(debts: List[Dict[str, Any]]) -> None:
    """
    Sporcu / Kişi Bazlı Cari Hesap Özeti ve Detaylı Döküm ekranını render eder.

    Args:
        debts: Mevcut borç/alacak kayıtları listesi.
    """
    st.markdown("""
    <style>
    .ledger-card {
        background: rgba(255, 255, 255, 0.85);
        border: 1px solid #E2E8F0;
        border-radius: 14px;
        padding: 16px 18px;
        margin-bottom: 12px;
        transition: all 0.2s ease;
        box-shadow: 0 2px 6px rgba(0, 0, 0, 0.02);
    }
    .ledger-card:hover {
        border-color: #CBD5E1;
        box-shadow: 0 6px 14px rgba(0, 0, 0, 0.05);
    }
    @media (prefers-color-scheme: dark) {
        .ledger-card {
            background: rgba(30, 41, 59, 0.75);
            border-color: #334155;
        }
        .ledger-card:hover {
            border-color: #475569;
        }
    }
    .ledger-pill {
        display: inline-flex;
        align-items: center;
        gap: 4px;
        padding: 3px 10px;
        border-radius: 999px;
        font-size: 0.74rem;
        font-weight: 700;
    }
    </style>
    """, unsafe_allow_html=True)

    if not debts:
        st.info("📭 Cari hesap özeti oluşturmak için henüz kayıtlı bir borç veya alacak işlemi bulunmuyor.")
        return

    summaries = compute_member_summaries(debts)

    # --- 1. ÜST ÖZET METRİKLERİ ---
    debtor_members = [m for m in summaries if m["pending_receivable"] > 0]
    creditor_members = [m for m in summaries if m["pending_payable"] > 0]
    active_members = [m for m in summaries if m["pending_count"] > 0]

    total_receivable = sum(m["pending_receivable"] for m in summaries)
    total_payable = sum(m["pending_payable"] for m in summaries)
    team_net_balance = total_receivable - total_payable

    # En yüksek borçlu (Takımın en çok alacağı olan kişi) veya alacaklı
    top_debtor = max(summaries, key=lambda m: m["pending_receivable"], default=None)
    top_creditor = max(summaries, key=lambda m: m["pending_payable"], default=None)

    if top_debtor and top_debtor["pending_receivable"] > 0:
        top_person_title = "🏆 En Yüksek Borçlu (Alacağımız)"
        top_person_name = top_debtor["name"]
        top_person_val = f"+{top_debtor['pending_receivable']:,.2f} TL"
        top_person_color = "#10B981"
        if top_creditor and top_creditor["pending_payable"] > 0:
            top_person_sub = f"En Yüksek Alacaklı: {top_creditor['name']} (-{top_creditor['pending_payable']:,.2f} TL)"
        else:
            top_person_sub = f"{top_debtor['pending_count']} bekleyen işlem kalemi"
    elif top_creditor and top_creditor["pending_payable"] > 0:
        top_person_title = "🏦 En Yüksek Alacaklı (Borcumuz)"
        top_person_name = top_creditor["name"]
        top_person_val = f"-{top_creditor['pending_payable']:,.2f} TL"
        top_person_color = "#EF4444"
        top_person_sub = f"{top_creditor['pending_count']} bekleyen işlem kalemi"
    else:
        top_person_title = "🏅 En Yüksek Borçlu / Alacaklı"
        top_person_name = "Yok"
        top_person_val = "0,00 TL"
        top_person_color = "#64748B"
        top_person_sub = "Tüm cari hesaplar kapandı"

    net_color = "#10B981" if team_net_balance >= 0 else "#EF4444"
    net_sign = "+" if team_net_balance > 0 else ""

    m_col1, m_col2, m_col3 = st.columns(3)
    with m_col1:
        st.markdown(f"""
        <div class="metric-card" style="text-align: center;">
            <div class="metric-title">👥 Borçlu / Açık Hesap Kişi Sayısı</div>
            <div class="metric-value" style="color: #3B82F6;">{len(debtor_members)} Kişi</div>
            <div class="metric-sub">Aktif Cari: {len(active_members)} | Toplam Kayıtlı: {len(summaries)} Kişi</div>
        </div>
        """, unsafe_allow_html=True)

    with m_col2:
        st.markdown(f"""
        <div class="metric-card" style="text-align: center;">
            <div class="metric-title">{top_person_title}</div>
            <div class="metric-value" style="color: {top_person_color}; font-size: 1.25rem;">{top_person_name} ({top_person_val})</div>
            <div class="metric-sub">{top_person_sub}</div>
        </div>
        """, unsafe_allow_html=True)

    with m_col3:
        st.markdown(f"""
        <div class="metric-card" style="text-align: center;">
            <div class="metric-title">⚖️ Takım Net Alacak / Borç Dengesi</div>
            <div class="metric-value" style="color: {net_color};">{net_sign}{team_net_balance:,.2f} TL</div>
            <div class="metric-sub">Alınacak: +{total_receivable:,.2f} TL | Verilecek: -{total_payable:,.2f} TL</div>
        </div>
        """, unsafe_allow_html=True)

    st.markdown("<br>", unsafe_allow_html=True)

    # --- 2. ARAMA & FİLTRELEME KUTUSU ---
    f_col1, f_col2, f_col3 = st.columns([2, 1.2, 1.2])
    with f_col1:
        search_query = st.text_input(
            "🔍 Sporcu / Kişi Ara",
            placeholder="İsim veya kurum yazın (Örn: Ahmet Yılmaz)...",
            key="member_ledger_search_input"
        )
    with f_col2:
        status_filter = st.selectbox(
            "📌 Hesap Durumu Filtresi",
            options=[
                "Tümü (Aktif + Kapalı)",
                "Sadece Açık Hesaplar (Bekleyen)",
                "Takıma Borçlu Olanlar (Alınacak > 0)",
                "Takımın Borçlu Oldukları (Verilecek > 0)",
                "Vadesi Geçmiş Olanlar",
                "Hesabı Kapanmış Olanlar",
            ],
            index=0,
            key="member_ledger_status_filter"
        )
    with f_col3:
        sort_option = st.selectbox(
            "↕️ Sıralama Ölçütü",
            options=[
                "Öncelik & Bakiye Büyüklüğü",
                "Net Bakiye (Azalan)",
                "Net Bakiye (Artan)",
                "İsim (A - Z)",
                "İşlem Sayısı (Azalan)",
            ],
            index=0,
            key="member_ledger_sort_option"
        )

    # Filtreleri uygula
    filtered = summaries
    if search_query and search_query.strip():
        q = _turkish_sort_key(search_query.strip())
        filtered = [m for m in filtered if q in _turkish_sort_key(m["name"])]

    if status_filter == "Sadece Açık Hesaplar (Bekleyen)":
        filtered = [m for m in filtered if m["pending_count"] > 0]
    elif status_filter == "Takıma Borçlu Olanlar (Alınacak > 0)":
        filtered = [m for m in filtered if m["pending_receivable"] > 0]
    elif status_filter == "Takımın Borçlu Oldukları (Verilecek > 0)":
        filtered = [m for m in filtered if m["pending_payable"] > 0]
    elif status_filter == "Vadesi Geçmiş Olanlar":
        filtered = [m for m in filtered if m["overdue_count"] > 0]
    elif status_filter == "Hesabı Kapanmış Olanlar":
        filtered = [m for m in filtered if m["pending_count"] == 0]

    # Sıralamayı uygula
    if sort_option == "Net Bakiye (Azalan)":
        filtered.sort(key=lambda x: (-x["net_balance"], _turkish_sort_key(x["name"])))
    elif sort_option == "Net Bakiye (Artan)":
        filtered.sort(key=lambda x: (x["net_balance"], _turkish_sort_key(x["name"])))
    elif sort_option == "İsim (A - Z)":
        filtered.sort(key=lambda x: _turkish_sort_key(x["name"]))
    elif sort_option == "İşlem Sayısı (Azalan)":
        filtered.sort(key=lambda x: (-x["total_records"], _turkish_sort_key(x["name"])))

    # --- 3. KİŞİ BAZLI CARİ KARTLAR VE DETAYLI İNCELEME ---
    col_cards, col_detail = st.columns([1.35, 1.15])

    with col_cards:
        st.markdown(f"### 📇 Kişi / Sporcu Cari Kartları ({len(filtered)})")
        if not filtered:
            st.warning("Seçilen arama/filtre kriterlerine uygun sporcu veya kişi kaydı bulunamadı.")
        else:
            for m in filtered:
                net_val = m["net_balance"]
                if net_val > 0:
                    net_badge_bg = "rgba(16, 185, 129, 0.12)"
                    net_badge_color = "#10B981"
                    net_badge_border = "rgba(16, 185, 129, 0.3)"
                    net_label = f"+{net_val:,.2f} TL (Alacağımız)"
                    left_border = "#10B981"
                elif net_val < 0:
                    net_badge_bg = "rgba(239, 68, 68, 0.12)"
                    net_badge_color = "#EF4444"
                    net_badge_border = "rgba(239, 68, 68, 0.3)"
                    net_label = f"{net_val:,.2f} TL (Borcumuz)"
                    left_border = "#EF4444"
                else:
                    net_badge_bg = "rgba(100, 116, 139, 0.12)"
                    net_badge_color = "#64748B"
                    net_badge_border = "rgba(100, 116, 139, 0.25)"
                    net_label = "0,00 TL (Dengede / Kapalı)"
                    left_border = "#94A3B8"

                if m["overdue_count"] > 0:
                    vade_summary_html = (
                        f"<span class='ledger-pill' style='background: rgba(239,68,68,0.14); "
                        f"color: #EF4444; border: 1px solid rgba(239,68,68,0.3);'>"
                        f"🚨 {m['overdue_count']} Gecikmiş Vade</span>"
                    )
                elif m["nearest_due_date"]:
                    due_info = _get_due_status_info(m["nearest_due_date"])
                    vade_summary_html = due_info["badge_html"]
                elif m["pending_count"] > 0:
                    vade_summary_html = (
                        "<span class='ledger-pill' style='background: rgba(59,130,246,0.1); "
                        "color: #3B82F6;'>⏳ Açık Hesap (Vadesiz)</span>"
                    )
                else:
                    vade_summary_html = (
                        "<span class='ledger-pill' style='background: rgba(16,185,129,0.1); "
                        "color: #10B981;'>✅ Tüm Borçlar Ödendi</span>"
                    )

                st.markdown(f"""
                <div class="ledger-card" style="border-left: 5px solid {left_border};">
                    <div style="display: flex; justify-content: space-between; align-items: flex-start; gap: 10px; flex-wrap: wrap;">
                        <div>
                            <div style="font-weight: 700; font-size: 1.0rem; display: flex; align-items: center; gap: 8px;">
                                <span>🏃 {m['name']}</span>
                                {vade_summary_html}
                            </div>
                            <div style="font-size: 0.78rem; color: #64748B; margin-top: 6px; display: flex; flex-wrap: wrap; gap: 12px;">
                                <span>🟢 <b>Bekleyen Alınacak:</b> {m['pending_receivable']:,.2f} TL</span>
                                <span>🔴 <b>Bekleyen Verilecek:</b> {m['pending_payable']:,.2f} TL</span>
                                <span>☑️ <b>Tamamlanan İşlem:</b> {m['paid_count']} adet</span>
                            </div>
                        </div>
                        <div style="text-align: right;">
                            <div style="background: {net_badge_bg}; color: {net_badge_color}; border: 1px solid {net_badge_border}; padding: 5px 12px; border-radius: 10px; font-weight: 800; font-size: 0.86rem; display: inline-block;">
                                {net_label}
                            </div>
                            <div style="font-size: 0.72rem; color: #94A3B8; margin-top: 4px;">
                                Bekleyen: {m['pending_count']} | Toplam: {m['total_records']} kayıt
                            </div>
                        </div>
                    </div>
                </div>
                """, unsafe_allow_html=True)

    with col_detail:
        st.markdown("### 🔎 Kişi Cari Hesap Ekstresi")
        member_names = [m["name"] for m in filtered] if filtered else [m["name"] for m in summaries]

        selected_member_name = st.selectbox(
            "Detaylı Dökümünü Görmek İstediğiniz Kişiyi Seçin",
            options=member_names,
            key="member_ledger_detail_select"
        )

        selected_member = next((m for m in summaries if m["name"] == selected_member_name), None)
        if selected_member:
            net_v = selected_member["net_balance"]
            net_c = "#10B981" if net_v >= 0 else "#EF4444"
            net_s = "+" if net_v > 0 else ""

            st.markdown(f"""
            <div style="background: linear-gradient(135deg, rgba(59,130,246,0.08) 0%, rgba(139,92,246,0.08) 100%); border: 1px solid rgba(59,130,246,0.25); border-radius: 14px; padding: 16px; margin-bottom: 14px;">
                <div style="font-size: 1.05rem; font-weight: 800; margin-bottom: 8px;">
                    👤 {selected_member['name']} — Cari Hesap Özeti
                </div>
                <div style="display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; text-align: center;">
                    <div style="background: rgba(16,185,129,0.1); padding: 8px; border-radius: 10px;">
                        <div style="font-size: 0.72rem; color: #64748B; font-weight: 600;">Bekleyen Alınacak</div>
                        <div style="font-size: 0.92rem; font-weight: 800; color: #10B981;">+{selected_member['pending_receivable']:,.2f} TL</div>
                    </div>
                    <div style="background: rgba(239,68,68,0.1); padding: 8px; border-radius: 10px;">
                        <div style="font-size: 0.72rem; color: #64748B; font-weight: 600;">Bekleyen Verilecek</div>
                        <div style="font-size: 0.92rem; font-weight: 800; color: #EF4444;">-{selected_member['pending_payable']:,.2f} TL</div>
                    </div>
                    <div style="background: rgba(100,116,139,0.12); padding: 8px; border-radius: 10px;">
                        <div style="font-size: 0.72rem; color: #64748B; font-weight: 600;">Net Cari Bakiye</div>
                        <div style="font-size: 0.92rem; font-weight: 800; color: {net_c};">{net_s}{net_v:,.2f} TL</div>
                    </div>
                </div>
                <div style="font-size: 0.75rem; color: #64748B; margin-top: 10px; display: flex; justify-content: space-between;">
                    <span>⏳ Bekleyen Kalem: <b>{selected_member['pending_count']}</b></span>
                    <span>✅ Tamamlanmış / Ödenmiş: <b>{selected_member['paid_count']}</b></span>
                    <span>📊 Toplam İşlem: <b>{selected_member['total_records']}</b></span>
                </div>
            </div>
            """, unsafe_allow_html=True)

            pending_recs = [r for r in selected_member["records"] if r.get("status") == "Bekliyor"]
            paid_recs = [r for r in selected_member["records"] if r.get("status") == "Ödendi"]

            d_tab1, d_tab2 = st.tabs([
                f"⏳ Bekleyen Kalemler ({len(pending_recs)})",
                f"✅ Geçmiş / Ödenmiş ({len(paid_recs)})"
            ])

            with d_tab1:
                if not pending_recs:
                    st.success("🎉 Bu kişiye ait bekleyen borç veya alacak kalemi bulunmuyor.")
                else:
                    for r in pending_recs:
                        is_rec = r.get("type") == "Alınacak"
                        amt_color = "#10B981" if is_rec else "#EF4444"
                        sign = "+" if is_rec else "-"
                        type_tag = "🟢 Alınacak (Bize Ödeyecek)" if is_rec else "🔴 Verilecek (Biz Ödeyeceğiz)"
                        cat_emoji = r.get("category_emoji") or "🏷️"
                        cat_name = r.get("category_name") or "Kategorisiz"
                        due_str = r.get("due_date") or "Belirsiz"
                        badge_html = r["due_info"]["badge_html"]

                        st.markdown(f"""
                        <div style="border: 1px solid #E2E8F0; border-left: 4px solid {amt_color}; border-radius: 10px; padding: 10px 14px; margin-bottom: 8px;">
                            <div style="display: flex; justify-content: space-between; align-items: center;">
                                <span style="font-weight: 700; font-size: 0.85rem;">{cat_emoji} {cat_name} <code style="font-size:0.72rem;">#{r.get('id')}</code></span>
                                <span style="color: {amt_color}; font-weight: 800; font-size: 0.92rem;">{sign}{float(r.get('amount', 0)):,.2f} TL</span>
                            </div>
                            <div style="display: flex; justify-content: space-between; align-items: center; margin-top: 6px; font-size: 0.76rem; color: #64748B;">
                                <span>{type_tag} | 📅 Vade: <b>{due_str}</b></span>
                                {badge_html}
                            </div>
                        </div>
                        """, unsafe_allow_html=True)

            with d_tab2:
                if not paid_recs:
                    st.info("Bu kişiye ait tamamlanmış/ödenmiş geçmiş işlem bulunmuyor.")
                else:
                    for r in paid_recs:
                        is_rec = r.get("type") == "Alınacak"
                        type_tag = "Alınacak (Tahsil Edildi)" if is_rec else "Verilecek (Ödendi)"
                        cat_emoji = r.get("category_emoji") or "🏷️"
                        cat_name = r.get("category_name") or "Kategorisiz"
                        due_str = r.get("due_date") or "Belirsiz"

                        st.markdown(f"""
                        <div style="border: 1px solid #E2E8F0; border-left: 4px solid #94A3B8; border-radius: 10px; padding: 10px 14px; margin-bottom: 8px; opacity: 0.85;">
                            <div style="display: flex; justify-content: space-between; align-items: center;">
                                <span style="font-weight: 600; font-size: 0.84rem;">✅ {cat_emoji} {cat_name} <code style="font-size:0.72rem;">#{r.get('id')}</code></span>
                                <span style="color: #64748B; font-weight: 700; font-size: 0.88rem;">{float(r.get('amount', 0)):,.2f} TL</span>
                            </div>
                            <div style="display: flex; justify-content: space-between; align-items: center; margin-top: 4px; font-size: 0.74rem; color: #94A3B8;">
                                <span>{type_tag}</span>
                                <span>📅 Vade: {due_str}</span>
                            </div>
                        </div>
                        """, unsafe_allow_html=True)

    # --- 4. TABLO GÖRÜNÜMÜ (TÜM KİŞİLER ÖZET TABLOSU) ---
    with st.expander("📊 Tüm Sporcu / Kişi Cari Hesaplarını Tablo Olarak Görüntüle", expanded=False):
        table_rows = []
        for m in filtered:
            table_rows.append({
                "Kişi / Kurum Adı": m["name"],
                "Bekleyen Alınacak (TL)": m["pending_receivable"],
                "Bekleyen Verilecek (TL)": m["pending_payable"],
                "Net Cari Durum (TL)": m["net_balance"],
                "Bekleyen İşlem": m["pending_count"],
                "Gecikmiş Vade": m["overdue_count"],
                "Tamamlanan İşlem": m["paid_count"],
                "En Yakın Vade": m["nearest_due_date"] or "—",
            })
        if table_rows:
            df_ledger = pd.DataFrame(table_rows)
            st.dataframe(
                df_ledger,
                column_config={
                    "Bekleyen Alınacak (TL)": st.column_config.NumberColumn(format="%.2f TL"),
                    "Bekleyen Verilecek (TL)": st.column_config.NumberColumn(format="%.2f TL"),
                    "Net Cari Durum (TL)": st.column_config.NumberColumn(format="%.2f TL"),
                },
                hide_index=True,
                use_container_width=True,
            )
