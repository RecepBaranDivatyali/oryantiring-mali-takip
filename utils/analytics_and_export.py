import csv
import html
import io
from datetime import datetime, timedelta
import altair as alt
import pandas as pd
import streamlit as st


def format_tr_currency(amount: float, include_symbol: bool = True) -> str:
    """Sayısal değeri Türkçe para birimi formatına (1.234,56 TL) çevirir."""
    try:
        val = float(amount or 0.0)
    except (ValueError, TypeError):
        val = 0.0
    formatted = f"{val:,.2f}".replace(",", "TEMP").replace(".", ",").replace("TEMP", ".")
    return f"{formatted} TL" if include_symbol else formatted


def _enrich_transactions(transactions: list[dict], categories: list[dict]) -> list[dict]:
    """İşlem kayıtlarında eksik olabilecek kategori bilgilerini categories listesinden tamamlar."""
    cat_map = {c.get("id"): c for c in (categories or []) if c.get("id") is not None}
    enriched = []
    for tx in (transactions or []):
        item = dict(tx)
        cat = cat_map.get(item.get("category_id"))
        if not item.get("category_name"):
            item["category_name"] = (cat.get("name") if cat else None) or "Diğer"
        if not item.get("emoji"):
            item["emoji"] = (cat.get("emoji") if cat else None) or "📌"
        if not item.get("color"):
            item["color"] = (cat.get("color") if cat else None) or "#6366F1"
        if not item.get("type"):
            item["type"] = (cat.get("type") if cat else None) or "Gider"
        enriched.append(item)
    return enriched


def _filter_transactions_by_period(transactions: list[dict], period_label: str) -> list[dict]:
    """Seçilen döneme göre işlemleri filtreler."""
    if not transactions or period_label == "Tüm Zamanlar":
        return list(transactions or [])

    today = datetime.now().date()
    if period_label == "Bu Ay":
        start_date = today.replace(day=1)
    elif period_label == "Son 3 Ay":
        start_date = today - timedelta(days=90)
    elif period_label == "Bu Yıl":
        start_date = today.replace(month=1, day=1)
    else:
        return list(transactions)

    filtered = []
    for tx in transactions:
        date_str = str(tx.get("date", ""))[:10]
        try:
            tx_date = datetime.strptime(date_str, "%Y-%m-%d").date()
            if tx_date >= start_date:
                filtered.append(tx)
        except ValueError:
            continue
    return filtered


def _aggregate_by_category(transactions: list[dict], tx_type: str) -> list[dict]:
    """Belirli işlem tipine ('Gider' veya 'Gelir') göre kategori bazlı toplamları hesaplar."""
    fallback_palette = [
        "#EF4444", "#F59E0B", "#10B981", "#3B82F6", "#8B5CF6",
        "#EC4899", "#14B8A6", "#F97316", "#6366F1", "#06B6D4"
    ]
    grouped: dict[str, dict] = {}
    for idx, tx in enumerate(transactions):
        if tx.get("type") != tx_type:
            continue
        try:
            amt = float(tx.get("amount") or 0.0)
        except (ValueError, TypeError):
            amt = 0.0
        if amt <= 0:
            continue

        cat_name = tx.get("category_name") or "Diğer"
        emoji = tx.get("emoji") or ("💸" if tx_type == "Gider" else "💰")
        color = tx.get("color") or fallback_palette[idx % len(fallback_palette)]
        key = f"{cat_name}_{emoji}"

        if key not in grouped:
            grouped[key] = {
                "category_name": cat_name,
                "emoji": emoji,
                "display_label": f"{emoji} {cat_name}",
                "color": color,
                "tx_count": 0,
                "total_amount": 0.0,
            }
        grouped[key]["tx_count"] += 1
        grouped[key]["total_amount"] += amt

    rows = sorted(grouped.values(), key=lambda x: x["total_amount"], reverse=True)
    grand_total = sum(r["total_amount"] for r in rows)

    used_colors = set()
    for i, r in enumerate(rows):
        r["percentage"] = (r["total_amount"] / grand_total * 100.0) if grand_total > 0 else 0.0
        r["formatted_amount"] = format_tr_currency(r["total_amount"])
        r["formatted_pct"] = f"%{r['percentage']:.1f}"
        c = r["color"]
        if not c or c in used_colors:
            c = fallback_palette[i % len(fallback_palette)]
        used_colors.add(c)
        r["chart_color"] = c

    return rows


def _render_donut_and_spendee_list(rows: list[dict], tx_type: str, period_label: str) -> None:
    """Donut grafik ve yanında Spendee tarzı kategori ilerleme çubuklarını çizer."""
    is_expense = tx_type == "Gider"
    accent_color = "#EF4444" if is_expense else "#10B981"
    empty_icon = "🎉" if is_expense else "📭"
    empty_msg = (
        f"**{period_label}** döneminde kayıtlı gider işlemi bulunmuyor."
        if is_expense
        else f"**{period_label}** döneminde kayıtlı gelir işlemi bulunmuyor."
    )

    if not rows:
        st.markdown(
            f"""
            <div style="padding: 28px 20px; border-radius: 16px; border: 1px dashed rgba(148, 163, 184, 0.35);
                        background: rgba(148, 163, 184, 0.05); text-align: center; margin: 8px 0;">
                <div style="font-size: 2rem; margin-bottom: 6px;">{empty_icon}</div>
                <div style="font-size: 0.92rem; color: #64748B;">{empty_msg}</div>
            </div>
            """,
            unsafe_allow_html=True,
        )
        return

    grand_total = sum(r["total_amount"] for r in rows)
    total_tx_count = sum(r["tx_count"] for r in rows)

    col_chart, col_list = st.columns([1.05, 1.35], gap="large")

    with col_chart:
        df_chart = pd.DataFrame(rows)
        domain = df_chart["display_label"].tolist()
        color_range = df_chart["chart_color"].tolist()

        base = alt.Chart(df_chart).encode(
            theta=alt.Theta(field="total_amount", type="quantitative", stack=True),
            order=alt.Order(field="total_amount", sort="descending"),
            color=alt.Color(
                field="display_label",
                type="nominal",
                scale=alt.Scale(domain=domain, range=color_range),
                legend=alt.Legend(
                    orient="bottom",
                    title=None,
                    columns=2,
                    labelLimit=160,
                    symbolType="circle",
                ),
            ),
            tooltip=[
                alt.Tooltip("display_label:N", title="Kategori"),
                alt.Tooltip("formatted_amount:N", title="Toplam Tutar"),
                alt.Tooltip("formatted_pct:N", title="Oran"),
                alt.Tooltip("tx_count:Q", title="İşlem Sayısı"),
            ],
        )

        donut = base.mark_arc(
            innerRadius=68,
            outerRadius=118,
            cornerRadius=6,
            padAngle=0.015,
            stroke="#ffffff",
            strokeWidth=1.5,
        ).properties(height=300)

        st.altair_chart(donut, use_container_width=True)

        st.markdown(
            f"""
            <div style="text-align: center; margin-top: -8px; padding: 10px 14px; border-radius: 12px;
                        background: rgba(148, 163, 184, 0.08); border: 1px solid rgba(148, 163, 184, 0.18);">
                <div style="font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.06em; color: #64748B; font-weight: 600;">
                    Toplam {tx_type} ({total_tx_count} İşlem)
                </div>
                <div style="font-size: 1.3rem; font-weight: 800; color: {accent_color}; margin-top: 2px;">
                    {format_tr_currency(grand_total)}
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

    with col_list:
        cards_html = ['<div style="display: flex; flex-direction: column; gap: 10px; padding-top: 4px;">']
        for item in rows:
            cat_esc = html.escape(str(item["category_name"]))
            emoji_esc = html.escape(str(item["emoji"]))
            color_hex = html.escape(str(item["color"] or accent_color))
            pct_val = max(2.0, min(100.0, float(item["percentage"])))
            pct_str = f"%{item['percentage']:.1f}"
            amt_str = item["formatted_amount"]
            tx_cnt = item["tx_count"]

            card = (
                f'<div style="padding: 12px 14px; border-radius: 14px; '
                f'background: rgba(148, 163, 184, 0.06); '
                f'border: 1px solid rgba(148, 163, 184, 0.18);">'
                f'<div style="display: flex; justify-content: space-between; align-items: center; gap: 8px; margin-bottom: 8px;">'
                f'<div style="display: flex; align-items: center; gap: 10px; min-width: 0;">'
                f'<span style="width: 34px; height: 34px; border-radius: 10px; display: inline-flex; '
                f'align-items: center; justify-content: center; background: {color_hex}20; '
                f'border: 1px solid {color_hex}40; font-size: 1.05rem; flex-shrink: 0;">{emoji_esc}</span>'
                f'<div style="min-width: 0;">'
                f'<div style="font-weight: 700; font-size: 0.92rem; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;">{cat_esc}</div>'
                f'<div style="font-size: 0.74rem; color: #64748B;">{tx_cnt} işlem</div>'
                f'</div>'
                f'</div>'
                f'<div style="text-align: right; flex-shrink: 0;">'
                f'<div style="font-weight: 800; font-size: 0.95rem; color: {accent_color};">{amt_str}</div>'
                f'<div style="font-size: 0.75rem; font-weight: 700; color: {color_hex};">{pct_str}</div>'
                f'</div>'
                f'</div>'
                f'<div style="width: 100%; height: 8px; border-radius: 999px; background: rgba(148, 163, 184, 0.18); overflow: hidden;">'
                f'<div style="width: {pct_val:.2f}%; height: 100%; border-radius: 999px; background: {color_hex};"></div>'
                f'</div>'
                f'</div>'
            )
            cards_html.append(card)
        cards_html.append("</div>")
        st.markdown("".join(cards_html), unsafe_allow_html=True)


def render_category_analytics(transactions: list[dict], categories: list[dict]) -> None:
    """
    Spendee tarzı kategori bazlı harcama ve gelir dağılım analitiğini ekrana çizer.
    Dönem filtresi, Altair Donut Chart ve HTML progress bar kartları içerir.
    """
    enriched_txs = _enrich_transactions(transactions, categories)

    col_hdr, col_period = st.columns([1.2, 1.3], gap="medium")
    with col_hdr:
        st.markdown(
            """
            <div style="padding-top: 2px;">
                <div style="font-size: 1.1rem; font-weight: 800; letter-spacing: -0.01em;">
                    📊 Kategori Bazlı Gelir & Gider Dağılımı
                </div>
                <div style="font-size: 0.8rem; color: #64748B;">
                    Seçilen dönemdeki harcama ve gelir kalemlerinin oransal kırılımı
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )
    with col_period:
        period_options = ["Bu Ay", "Son 3 Ay", "Bu Yıl", "Tüm Zamanlar"]
        selected_period = st.radio(
            "Analiz Dönemi",
            options=period_options,
            index=3,
            horizontal=True,
            label_visibility="collapsed",
            key="analytics_period_selector",
        )

    filtered_txs = _filter_transactions_by_period(enriched_txs, selected_period)
    expense_rows = _aggregate_by_category(filtered_txs, "Gider")
    income_rows = _aggregate_by_category(filtered_txs, "Gelir")

    total_expense = sum(r["total_amount"] for r in expense_rows)
    total_income = sum(r["total_amount"] for r in income_rows)
    net_flow = total_income - total_expense
    net_color = "#10B981" if net_flow >= 0 else "#EF4444"
    net_sign = "+" if net_flow > 0 else ""

    st.markdown(
        f"""
        <div style="display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; margin: 10px 0 16px 0;">
            <div style="padding: 12px 14px; border-radius: 14px; background: rgba(239, 68, 68, 0.07); border: 1px solid rgba(239, 68, 68, 0.2);">
                <div style="font-size: 0.74rem; color: #64748B; font-weight: 600;">💸 Dönem Gideri ({selected_period})</div>
                <div style="font-size: 1.1rem; font-weight: 800; color: #EF4444; margin-top: 2px;">{format_tr_currency(total_expense)}</div>
            </div>
            <div style="padding: 12px 14px; border-radius: 14px; background: rgba(16, 185, 129, 0.07); border: 1px solid rgba(16, 185, 129, 0.2);">
                <div style="font-size: 0.74rem; color: #64748B; font-weight: 600;">💰 Dönem Geliri ({selected_period})</div>
                <div style="font-size: 1.1rem; font-weight: 800; color: #10B981; margin-top: 2px;">{format_tr_currency(total_income)}</div>
            </div>
            <div style="padding: 12px 14px; border-radius: 14px; background: rgba(148, 163, 184, 0.07); border: 1px solid rgba(148, 163, 184, 0.2);">
                <div style="font-size: 0.74rem; color: #64748B; font-weight: 600;">⚖️ Dönem Net Akış</div>
                <div style="font-size: 1.1rem; font-weight: 800; color: {net_color}; margin-top: 2px;">{net_sign}{format_tr_currency(net_flow)}</div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    tab_expense, tab_income = st.tabs(["💸 Gider Dağılımı", "💰 Gelir Dağılımı"])
    with tab_expense:
        _render_donut_and_spendee_list(expense_rows, "Gider", selected_period)
    with tab_income:
        _render_donut_and_spendee_list(income_rows, "Gelir", selected_period)


def _generate_transactions_csv_bytes(transactions: list[dict]) -> bytes:
    """Tüm işlemleri Türkçe Excel uyumlu (UTF-8-SIG BOM ve noktalı virgül ayraçlı) CSV byte dizisine dönüştürür."""
    output = io.StringIO()
    writer = csv.writer(output, delimiter=";", quoting=csv.QUOTE_MINIMAL)
    writer.writerow([
        "İşlem ID",
        "Tarih",
        "Saat Dilimi",
        "Tür",
        "Kategori",
        "Açıklama",
        "Tutar (TL)",
    ])
    for tx in (transactions or []):
        amt = float(tx.get("amount") or 0.0)
        signed_amt = amt if tx.get("type") == "Gelir" else -amt
        writer.writerow([
            tx.get("id", ""),
            tx.get("date", ""),
            tx.get("time_range", "05:00 - 18:15"),
            tx.get("type", ""),
            f"{tx.get('emoji', '')} {tx.get('category_name', '')}".strip(),
            tx.get("description") or "",
            format_tr_currency(signed_amt, include_symbol=False),
        ])
    return output.getvalue().encode("utf-8-sig")


def _generate_debts_csv_bytes(debts: list[dict]) -> bytes:
    """Borç ve alacak listesini Türkçe Excel uyumlu (UTF-8-SIG BOM ve noktalı virgül ayraçlı) CSV byte dizisine dönüştürür."""
    output = io.StringIO()
    writer = csv.writer(output, delimiter=";", quoting=csv.QUOTE_MINIMAL)
    writer.writerow([
        "Kayıt ID",
        "Tür",
        "Kişi / Kurum / Açıklama",
        "Kategori",
        "Vade Tarihi",
        "Durum",
        "Tutar (TL)",
    ])
    for d in (debts or []):
        amt = float(d.get("amount") or 0.0)
        cat_label = f"{d.get('category_emoji') or ''} {d.get('category_name') or 'Genel'}".strip()
        writer.writerow([
            d.get("id", ""),
            d.get("type", ""),
            d.get("name", ""),
            cat_label,
            d.get("due_date") or "Belirtilmedi",
            d.get("status", ""),
            format_tr_currency(amt, include_symbol=False),
        ])
    return output.getvalue().encode("utf-8-sig")


def generate_official_balance_sheet_html(
    transactions: list[dict],
    debts: list[dict],
    current_vault: float,
    total_interest_earned: float,
    accumulated_interest: float,
    monthly_flow: dict,
) -> str:
    """
    ODTÜ Oryantiring Takımı için tarayıcıda açılıp doğrudan PDF olarak yazdırılabilen
    resmi görünümlü HTML mali bilanço raporu üretir.
    """
    now_str = datetime.now().strftime("%d.%m.%Y %H:%M")
    transactions = list(transactions or [])
    debts = list(debts or [])
    monthly_flow = dict(monthly_flow or {"Gelir": 0.0, "Gider": 0.0})

    pending_receivables = sum(
        float(d.get("amount") or 0.0)
        for d in debts
        if d.get("status") == "Bekliyor" and d.get("type") == "Alınacak"
    )
    pending_payables = sum(
        float(d.get("amount") or 0.0)
        for d in debts
        if d.get("status") == "Bekliyor" and d.get("type") == "Verilecek"
    )
    net_worth = float(current_vault or 0.0) + pending_receivables - pending_payables

    total_income_all = sum(
        float(t.get("amount") or 0.0) for t in transactions if t.get("type") == "Gelir"
    )
    total_expense_all = sum(
        float(t.get("amount") or 0.0) for t in transactions if t.get("type") == "Gider"
    )

    expense_summary = _aggregate_by_category(transactions, "Gider")
    income_summary = _aggregate_by_category(transactions, "Gelir")

    def _build_cat_rows_html(rows: list[dict], empty_label: str) -> str:
        if not rows:
            return f'<tr><td colspan="4" style="text-align:center; color:#64748B; padding:12px;">{empty_label}</td></tr>'
        out = []
        for r in rows:
            cat_title = html.escape(f"{r['emoji']} {r['category_name']}")
            out.append(
                f"<tr>"
                f"<td>{cat_title}</td>"
                f"<td style='text-align:center;'>{r['tx_count']}</td>"
                f"<td style='text-align:right;'>%{r['percentage']:.1f}</td>"
                f"<td style='text-align:right; font-weight:700;'>{format_tr_currency(r['total_amount'])}</td>"
                f"</tr>"
            )
        return "".join(out)

    pending_debts = [d for d in debts if d.get("status") == "Bekliyor"]
    if pending_debts:
        debt_rows_list = []
        for d in pending_debts:
            d_type = html.escape(str(d.get("type") or ""))
            badge_cls = "badge-green" if d_type == "Alınacak" else "badge-red"
            d_name = html.escape(str(d.get("name") or ""))
            d_due = html.escape(str(d.get("due_date") or "Belirtilmedi"))
            d_amt = format_tr_currency(float(d.get("amount") or 0.0))
            debt_rows_list.append(
                f"<tr>"
                f"<td><span class='badge {badge_cls}'>{d_type}</span></td>"
                f"<td>{d_name}</td>"
                f"<td>{d_due}</td>"
                f"<td style='text-align:right; font-weight:700;'>{d_amt}</td>"
                f"</tr>"
            )
        debt_rows_html = "".join(debt_rows_list)
    else:
        debt_rows_html = '<tr><td colspan="4" style="text-align:center; color:#64748B; padding:12px;">Bekleyen borç veya alacak kaydı bulunmamaktadır.</td></tr>'

    recent_txs = transactions[:50]
    if recent_txs:
        tx_rows_list = []
        for tx in recent_txs:
            t_date = html.escape(str(tx.get("date") or ""))
            t_type = html.escape(str(tx.get("type") or ""))
            badge_cls = "badge-green" if t_type == "Gelir" else "badge-red"
            sign = "+" if t_type == "Gelir" else "-"
            t_cat = html.escape(f"{tx.get('emoji') or ''} {tx.get('category_name') or ''}".strip())
            t_desc = html.escape(str(tx.get("description") or "-"))
            t_amt = format_tr_currency(float(tx.get("amount") or 0.0))
            tx_rows_list.append(
                f"<tr>"
                f"<td>{t_date}</td>"
                f"<td><span class='badge {badge_cls}'>{t_type}</span></td>"
                f"<td>{t_cat}</td>"
                f"<td>{t_desc}</td>"
                f"<td style='text-align:right; font-weight:700;'>{sign} {t_amt}</td>"
                f"</tr>"
            )
        tx_rows_html = "".join(tx_rows_list)
    else:
        tx_rows_html = '<tr><td colspan="5" style="text-align:center; color:#64748B; padding:12px;">Kayıtlı işlem bulunmamaktadır.</td></tr>'

    month_inc = float(monthly_flow.get("Gelir") or 0.0)
    month_exp = float(monthly_flow.get("Gider") or 0.0)

    return f"""<!DOCTYPE html>
<html lang="tr">
<head>
    <meta charset="UTF-8">
    <title>ODTÜ Oryantiring Takımı - Resmi Mali Bilanço Raporu</title>
    <style>
        :root {{
            --primary: #990000;
            --dark: #0F172A;
            --muted: #475569;
            --border: #CBD5E1;
            --bg-light: #F8FAFC;
        }}
        * {{
            box-sizing: border-box;
            font-family: 'Segoe UI', -apple-system, BlinkMacSystemFont, Roboto, Arial, sans-serif;
        }}
        body {{
            margin: 0;
            padding: 28px;
            background: #F1F5F9;
            color: var(--dark);
        }}
        .report-container {{
            max-width: 980px;
            margin: 0 auto;
            background: #FFFFFF;
            padding: 36px 42px;
            border-radius: 14px;
            box-shadow: 0 10px 30px rgba(15, 23, 42, 0.08);
            border: 1px solid #E2E8F0;
        }}
        .toolbar {{
            display: flex;
            justify-content: flex-end;
            margin-bottom: 18px;
        }}
        .print-btn {{
            background: var(--primary);
            color: #FFFFFF;
            border: none;
            padding: 10px 18px;
            border-radius: 8px;
            font-size: 0.9rem;
            font-weight: 700;
            cursor: pointer;
            box-shadow: 0 4px 12px rgba(153, 0, 0, 0.25);
        }}
        .print-btn:hover {{
            opacity: 0.92;
        }}
        .header {{
            display: flex;
            justify-content: space-between;
            align-items: flex-start;
            border-bottom: 3px solid var(--primary);
            padding-bottom: 16px;
            margin-bottom: 24px;
        }}
        .header h1 {{
            margin: 0;
            font-size: 1.55rem;
            color: var(--primary);
            letter-spacing: -0.02em;
        }}
        .header p {{
            margin: 4px 0 0 0;
            color: var(--muted);
            font-size: 0.9rem;
        }}
        .meta-box {{
            text-align: right;
            font-size: 0.84rem;
            color: var(--muted);
            line-height: 1.5;
        }}
        .kpi-grid {{
            display: grid;
            grid-template-columns: repeat(3, 1fr);
            gap: 14px;
            margin-bottom: 26px;
        }}
        .kpi-card {{
            padding: 14px 16px;
            border-radius: 10px;
            background: var(--bg-light);
            border: 1px solid var(--border);
        }}
        .kpi-card.highlight {{
            background: #FEF2F2;
            border-color: #FECACA;
        }}
        .kpi-label {{
            font-size: 0.76rem;
            text-transform: uppercase;
            letter-spacing: 0.04em;
            color: var(--muted);
            font-weight: 700;
        }}
        .kpi-value {{
            font-size: 1.25rem;
            font-weight: 800;
            margin-top: 4px;
            color: var(--dark);
        }}
        .kpi-sub {{
            font-size: 0.75rem;
            color: var(--muted);
            margin-top: 2px;
        }}
        h2.section-title {{
            font-size: 1.05rem;
            margin: 26px 0 10px 0;
            padding-bottom: 6px;
            border-bottom: 1px solid var(--border);
            color: var(--dark);
        }}
        .two-col {{
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 18px;
        }}
        table {{
            width: 100%;
            border-collapse: collapse;
            font-size: 0.84rem;
            margin-bottom: 12px;
        }}
        th, td {{
            padding: 9px 10px;
            border-bottom: 1px solid #E2E8F0;
            text-align: left;
        }}
        th {{
            background: #F8FAFC;
            font-weight: 700;
            color: #334155;
            border-top: 1px solid #CBD5E1;
            border-bottom: 2px solid #CBD5E1;
        }}
        .badge {{
            display: inline-block;
            padding: 2px 8px;
            border-radius: 999px;
            font-size: 0.72rem;
            font-weight: 700;
        }}
        .badge-green {{
            background: #DCFCE7;
            color: #166534;
        }}
        .badge-red {{
            background: #FEE2E2;
            color: #991B1B;
        }}
        .signatures {{
            margin-top: 42px;
            padding-top: 18px;
            border-top: 1px dashed var(--border);
            display: grid;
            grid-template-columns: repeat(3, 1fr);
            gap: 24px;
            text-align: center;
            font-size: 0.85rem;
            color: var(--muted);
        }}
        .sig-line {{
            margin-top: 44px;
            border-top: 1px solid #94A3B8;
            padding-top: 6px;
            font-weight: 600;
            color: var(--dark);
        }}
        @media print {{
            body {{
                background: #FFFFFF;
                padding: 0;
            }}
            .report-container {{
                box-shadow: none;
                border: none;
                padding: 10px;
                max-width: 100%;
            }}
            .toolbar {{
                display: none !important;
            }}
        }}
    </style>
</head>
<body>
    <div class="report-container">
        <div class="toolbar">
            <button class="print-btn" onclick="window.print()">🖨️ Yazdır / PDF Olarak Kaydet</button>
        </div>

        <div class="header">
            <div>
                <h1>🧭 ODTÜ Oryantiring Takımı</h1>
                <p>Resmi Mali Bilanço, Kasa &amp; Kategori Hesap Özeti Raporu (SKS / Kulüp Yönetimi)</p>
            </div>
            <div class="meta-box">
                <div><strong>Rapor Tarihi:</strong> {now_str}</div>
                <div><strong>Toplam İşlem Kaydı:</strong> {len(transactions)} adet</div>
                <div><strong>Bekleyen Borç/Alacak:</strong> {len(pending_debts)} kayıt</div>
            </div>
        </div>

        <div class="kpi-grid">
            <div class="kpi-card">
                <div class="kpi-label">Güncel Kasa Bakiyesi</div>
                <div class="kpi-value">{format_tr_currency(current_vault)}</div>
                <div class="kpi-sub">Nakit + Mevduat Ana Kasa</div>
            </div>
            <div class="kpi-card">
                <div class="kpi-label">Toplam Birikmiş Net Faiz</div>
                <div class="kpi-value" style="color: #166534;">+ {format_tr_currency(total_interest_earned)}</div>
                <div class="kpi-sub">Valör Bekleyen: + {format_tr_currency(accumulated_interest)}</div>
            </div>
            <div class="kpi-card highlight">
                <div class="kpi-label">Net Varlık Durumu</div>
                <div class="kpi-value" style="color: #990000;">{format_tr_currency(net_worth)}</div>
                <div class="kpi-sub">Kasa + Alacaklar - Borçlar</div>
            </div>
            <div class="kpi-card">
                <div class="kpi-label">Bekleyen Alacaklar</div>
                <div class="kpi-value" style="color: #0284C7;">+ {format_tr_currency(pending_receivables)}</div>
                <div class="kpi-sub">Tahsil Edilecek Kayıtlar</div>
            </div>
            <div class="kpi-card">
                <div class="kpi-label">Bekleyen Borçlar</div>
                <div class="kpi-value" style="color: #DC2626;">- {format_tr_currency(pending_payables)}</div>
                <div class="kpi-sub">Ödenecek Yükümlülükler</div>
            </div>
            <div class="kpi-card">
                <div class="kpi-label">Bu Ay Nakit Akışı</div>
                <div class="kpi-value">{format_tr_currency(month_inc - month_exp)}</div>
                <div class="kpi-sub">Gelir: {format_tr_currency(month_inc)} | Gider: {format_tr_currency(month_exp)}</div>
            </div>
        </div>

        <h2 class="section-title">1. Kategori Bazlı Mali Dağılım Özeti (Tüm Zamanlar)</h2>
        <div class="two-col">
            <div>
                <h3 style="font-size:0.9rem; color:#991B1B; margin:6px 0;">💸 Gider Kalemleri (Toplam: {format_tr_currency(total_expense_all)})</h3>
                <table>
                    <thead>
                        <tr>
                            <th>Kategori</th>
                            <th style="text-align:center;">İşlem</th>
                            <th style="text-align:right;">Pay</th>
                            <th style="text-align:right;">Tutar</th>
                        </tr>
                    </thead>
                    <tbody>
                        {_build_cat_rows_html(expense_summary, "Gider kaydı bulunmuyor.")}
                    </tbody>
                </table>
            </div>
            <div>
                <h3 style="font-size:0.9rem; color:#166534; margin:6px 0;">💰 Gelir Kalemleri (Toplam: {format_tr_currency(total_income_all)})</h3>
                <table>
                    <thead>
                        <tr>
                            <th>Kategori</th>
                            <th style="text-align:center;">İşlem</th>
                            <th style="text-align:right;">Pay</th>
                            <th style="text-align:right;">Tutar</th>
                        </tr>
                    </thead>
                    <tbody>
                        {_build_cat_rows_html(income_summary, "Gelir kaydı bulunmuyor.")}
                    </tbody>
                </table>
            </div>
        </div>

        <h2 class="section-title">2. Bekleyen Borç ve Alacak Listesi</h2>
        <table>
            <thead>
                <tr>
                    <th style="width:110px;">Tür</th>
                    <th>Kişi / Kurum / Açıklama</th>
                    <th style="width:130px;">Vade Tarihi</th>
                    <th style="width:150px; text-align:right;">Tutar</th>
                </tr>
            </thead>
            <tbody>
                {debt_rows_html}
            </tbody>
        </table>

        <h2 class="section-title">3. Son İşlemler Dökümü (Son {len(recent_txs)} Kayıt)</h2>
        <table>
            <thead>
                <tr>
                    <th style="width:105px;">Tarih</th>
                    <th style="width:85px;">Tür</th>
                    <th style="width:180px;">Kategori</th>
                    <th>Açıklama</th>
                    <th style="width:150px; text-align:right;">Tutar</th>
                </tr>
            </thead>
            <tbody>
                {tx_rows_html}
            </tbody>
        </table>

        <div class="signatures">
            <div>
                <div>Hazırlayan</div>
                <div class="sig-line">Mali İşler Sorumlusu</div>
            </div>
            <div>
                <div>Kontrol Eden</div>
                <div class="sig-line">Takım Kaptanı / Yönetim</div>
            </div>
            <div>
                <div>Onaylayan</div>
                <div class="sig-line">Topluluk / Akademik Danışman</div>
            </div>
        </div>
    </div>
</body>
</html>"""


def render_export_and_reports(
    transactions: list[dict],
    debts: list[dict],
    current_vault: float,
    total_interest_earned: float,
    accumulated_interest: float,
    monthly_flow: dict,
) -> None:
    """
    SKS / Kulüp Yönetimi Resmi Bilanço Raporu ve Türkçe Excel uyumlu CSV indirme panelini çizer.
    """
    transactions = list(transactions or [])
    debts = list(debts or [])
    monthly_flow = dict(monthly_flow or {"Gelir": 0.0, "Gider": 0.0})

    pending_receivables = sum(
        float(d.get("amount") or 0.0)
        for d in debts
        if d.get("status") == "Bekliyor" and d.get("type") == "Alınacak"
    )
    pending_payables = sum(
        float(d.get("amount") or 0.0)
        for d in debts
        if d.get("status") == "Bekliyor" and d.get("type") == "Verilecek"
    )
    net_worth = float(current_vault or 0.0) + pending_receivables - pending_payables

    st.markdown(
        f"""
        <div style="padding: 16px 18px; border-radius: 16px; background: rgba(148, 163, 184, 0.06);
                    border: 1px solid rgba(148, 163, 184, 0.2); margin-bottom: 16px;">
            <div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 10px; margin-bottom: 12px;">
                <div>
                    <div style="font-size: 1.05rem; font-weight: 800;">🏛️ SKS &amp; Kulüp Yönetimi Mali Bilanço Özeti</div>
                    <div style="font-size: 0.8rem; color: #64748B;">
                        Excel (UTF-8 BOM) veri dışa aktarımı ve doğrudan PDF olarak yazdırılabilir resmi bilanço belgesi
                    </div>
                </div>
                <div style="padding: 6px 12px; border-radius: 999px; background: rgba(16, 185, 129, 0.12);
                            color: #10B981; font-size: 0.78rem; font-weight: 700;">
                    Net Varlık: {format_tr_currency(net_worth)}
                </div>
            </div>
            <div style="display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px;">
                <div style="padding: 10px 12px; border-radius: 12px; background: rgba(148, 163, 184, 0.08);">
                    <div style="font-size: 0.72rem; color: #64748B;">Güncel Kasa</div>
                    <div style="font-size: 0.98rem; font-weight: 800; margin-top: 2px;">{format_tr_currency(current_vault)}</div>
                </div>
                <div style="padding: 10px 12px; border-radius: 12px; background: rgba(16, 185, 129, 0.08);">
                    <div style="font-size: 0.72rem; color: #64748B;">Birikmiş Net Faiz</div>
                    <div style="font-size: 0.98rem; font-weight: 800; color: #10B981; margin-top: 2px;">+ {format_tr_currency(total_interest_earned)}</div>
                </div>
                <div style="padding: 10px 12px; border-radius: 12px; background: rgba(59, 130, 246, 0.08);">
                    <div style="font-size: 0.72rem; color: #64748B;">Bekleyen Alacaklar</div>
                    <div style="font-size: 0.98rem; font-weight: 800; color: #3B82F6; margin-top: 2px;">+ {format_tr_currency(pending_receivables)}</div>
                </div>
                <div style="padding: 10px 12px; border-radius: 12px; background: rgba(239, 68, 68, 0.08);">
                    <div style="font-size: 0.72rem; color: #64748B;">Bekleyen Borçlar</div>
                    <div style="font-size: 0.98rem; font-weight: 800; color: #EF4444; margin-top: 2px;">- {format_tr_currency(pending_payables)}</div>
                </div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    today_tag = datetime.now().strftime("%Y_%m_%d")
    tx_csv_bytes = _generate_transactions_csv_bytes(transactions)
    debts_csv_bytes = _generate_debts_csv_bytes(debts)
    report_html_str = generate_official_balance_sheet_html(
        transactions=transactions,
        debts=debts,
        current_vault=current_vault,
        total_interest_earned=total_interest_earned,
        accumulated_interest=accumulated_interest,
        monthly_flow=monthly_flow,
    )

    c1, c2, c3 = st.columns(3, gap="medium")
    with c1:
        st.download_button(
            label="📥 Tüm İşlemleri İndir (Excel / CSV)",
            data=tx_csv_bytes,
            file_name=f"odtu_oryantiring_islemler_{today_tag}.csv",
            mime="text/csv",
            use_container_width=True,
            key="dl_btn_all_transactions_csv",
        )
    with c2:
        st.download_button(
            label="📥 Borç & Alacak Listesini İndir (Excel / CSV)",
            data=debts_csv_bytes,
            file_name=f"odtu_oryantiring_borc_alacak_{today_tag}.csv",
            mime="text/csv",
            use_container_width=True,
            key="dl_btn_debts_csv",
        )
    with c3:
        st.download_button(
            label="📄 Resmi Mali Bilanço Raporu İndir (HTML/Yazdırılabilir)",
            data=report_html_str.encode("utf-8"),
            file_name=f"odtu_oryantiring_resmi_bilanco_{today_tag}.html",
            mime="text/html",
            use_container_width=True,
            type="primary",
            key="dl_btn_official_html_report",
        )
