import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter
from datetime import date
from io import BytesIO

def generate_payments_excel(payments_data, title="O'quvchilar To'lovlar Jadvali", subtitle="", stats=None):
    """
    HUMO O'quv markazi to'lovlar jadvalini openpyxl orqali
    chiroyli formatda Excel (.xlsx) sifatida yaratadi.
    Kechikkanlar (kun soni bilan), belgilangan to'lov sanasi, to'langan sana,
    kunlik, oylik va yillik moliyaviy hisobotlarni o'z ichiga oladi.
    """
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "To'lovlar"

    # Enable grid lines visibility
    ws.views.sheetView[0].showGridLines = True

    # Palitra ranglari
    c_title_bg = "1E293B"      # Deep Slate 900
    c_subtitle_bg = "334155"   # Slate 700
    c_stats_bg = "0F172A"      # Slate 950
    c_header_bg = "4338CA"     # Indigo 700
    c_border = "CBD5E1"        # Slate 300
    c_zebra_bg = "F8FAFC"      # Slate 50
    c_white = "FFFFFF"
    
    # Status ranglari
    c_paid_bg = "DCFCE7"       # Green 100
    c_paid_text = "15803D"     # Green 700
    c_unpaid_bg = "FEE2E2"     # Red 100
    c_unpaid_text = "B91C1C"   # Red 700
    c_partial_bg = "FEF3C7"    # Amber 100
    c_partial_text = "B45309"  # Amber 700
    c_overdue_bg = "FFE4E6"    # Rose 100
    c_overdue_text = "BE123C"  # Rose 700

    thin_side = Side(border_style="thin", color=c_border)
    thick_bottom = Side(border_style="medium", color="1E293B")
    double_bottom = Side(border_style="double", color="1E293B")

    cell_border = Border(left=thin_side, right=thin_side, top=thin_side, bottom=thin_side)
    header_border = Border(left=thin_side, right=thin_side, top=thin_side, bottom=thick_bottom)
    total_border = Border(left=thin_side, right=thin_side, top=thin_side, bottom=double_bottom)

    # 1. Asosiy sarlavha (Title)
    ws.merge_cells("A1:M1")
    title_cell = ws["A1"]
    title_cell.value = "HUMO TA'LIM MARKAZI — " + title.upper()
    title_cell.font = Font(name="Arial", size=14, bold=True, color="FFFFFF")
    title_cell.fill = PatternFill(start_color=c_title_bg, end_color=c_title_bg, fill_type="solid")
    title_cell.alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[1].height = 36

    # 2. Quyi sarlavha (Subtitle: Davr, sana, guruh)
    ws.merge_cells("A2:M2")
    sub_cell = ws["A2"]
    sub_cell.value = subtitle if subtitle else f"Hujjat yaratilgan sana: {date.today().strftime('%d.%m.%Y')}"
    sub_cell.font = Font(name="Arial", size=10, italic=True, color="E2E8F0")
    sub_cell.fill = PatternFill(start_color=c_subtitle_bg, end_color=c_subtitle_bg, fill_type="solid")
    sub_cell.alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[2].height = 24

    # 3. Moliyaviy ko'rsatkichlar sarlavhasi (Stats summary row)
    start_row = 3
    if stats:
        ws.merge_cells("A3:M3")
        stats_cell = ws["A3"]
        daily = stats.get('daily_collected_formatted', '0')
        monthly = stats.get('total_paid_formatted', '0')
        yearly = stats.get('yearly_collected_formatted', '0')
        debt = stats.get('debt_sum_formatted', '0')
        overdue_cnt = stats.get('overdue_count', 0)

        stats_cell.value = (
            f"MOLIYA: [ Bugungi tushum: {daily} so'm ] | [ Oylik tushum: {monthly} so'm ] | "
            f"[ Yillik umumiy: {yearly} so'm ] | [ Jami qarz: {debt} so'm ] | [ Kechikkanlar: {overdue_cnt} ta ]"
        )
        stats_cell.font = Font(name="Arial", size=9.5, bold=True, color="38BDF8")
        stats_cell.fill = PatternFill(start_color=c_stats_bg, end_color=c_stats_bg, fill_type="solid")
        stats_cell.alignment = Alignment(horizontal="center", vertical="center")
        ws.row_dimensions[3].height = 24
        start_row = 4

    # Bo'sh qator (separator)
    ws.row_dimensions[start_row].height = 8
    header_row = start_row + 1

    # 4. Ustunlar sarlavhalari (Headers)
    headers = [
        ("T/r", 6, "center"),
        ("Fan nomi", 20, "left"),
        ("O'qituvchi", 22, "left"),
        ("Ism familiyasi", 26, "left"),
        ("Kelgan sana", 14, "center"),
        ("To'lov muddati", 15, "center"),
        ("Kechikish holati", 18, "center"),
        ("To'langan sana", 15, "center"),
        ("Oylik tarif (so'm)", 18, "right"),
        ("To'langan miqdor", 18, "right"),
        ("To'lov holati", 16, "center"),
        ("Telefon raqami", 20, "center"),
        ("Izoh", 24, "left"),
    ]

    ws.row_dimensions[header_row].height = 28

    for col_idx, (col_name, _, align_h) in enumerate(headers, start=1):
        cell = ws.cell(row=header_row, column=col_idx, value=col_name)
        cell.font = Font(name="Arial", size=10, bold=True, color="FFFFFF")
        cell.fill = PatternFill(start_color=c_header_bg, end_color=c_header_bg, fill_type="solid")
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = header_border

    # 5. Ma'lumot qatorlari
    current_row = header_row + 1
    total_rate = 0
    total_paid = 0
    paid_count = 0
    unpaid_count = 0
    overdue_count = 0

    for item in payments_data:
        ws.row_dimensions[current_row].height = 22
        is_zebra = (current_row % 2 == 1)
        row_bg = c_zebra_bg if is_zebra else c_white

        # To'lov summasi raqamga aylantirish
        raw_paid = str(item.get('amount_paid', '')).strip()
        num_paid = 0
        cleaned_paid = raw_paid.replace(' ', '').replace("'", "").replace(',', '')
        if cleaned_paid.isdigit():
            num_paid = int(cleaned_paid)

        rate_val = item.get('payment_rate', 0) or 0
        total_rate += rate_val
        total_paid += num_paid

        st_code = item.get('status_code', 'unpaid')
        is_ovd = item.get('is_overdue', False)
        delay_days = item.get('delay_days', 0)

        if st_code == 'paid':
            paid_count += 1
        else:
            unpaid_count += 1
            if is_ovd:
                overdue_count += 1

        st_name = item.get('student_name', '-')
        if item.get('is_local'):
            st_name = f"{st_name} [Mahalladan]"

        debt_f = item.get('debt_formatted', '')
        if st_code == 'paid':
            status_txt = "To'langan"
        elif st_code == 'partial':
            status_txt = f"Qisman (Qarz: {debt_f} so'm)"
        else:
            rate_f = item.get('payment_rate_formatted', '')
            status_txt = f"Qarzdor ({rate_f} so'm)"

        # Kechikish matni
        if is_ovd:
            delay_txt = f"⚠️ {delay_days} kun kechikdi"
        elif st_code == 'paid':
            delay_txt = "To'langan"
        else:
            delay_txt = "Muddati kelmadi"

        # Ustun qiymatlari
        values = [
            (item.get('index', current_row - header_row), "center", None),
            (item.get('subject_name', '-'), "left", None),
            (item.get('teacher_name', '-'), "left", None),
            (st_name, "left", Font(name="Arial", size=10, bold=True)),
            (item.get('joined_date', '-'), "center", None),
            (item.get('due_date_str', '-'), "center", None),
            (delay_txt, "center", None),
            (item.get('payment_date', '-'), "center", None),
            (rate_val, "right", None, "#,##0"),
            (num_paid if num_paid > 0 else (raw_paid if raw_paid else "-"), "right", None, "#,##0" if num_paid > 0 else None),
            (status_txt, "center", None),
            (item.get('phone', '-'), "center", None),
            (item.get('notes', ''), "left", None),
        ]

        for col_idx, val_info in enumerate(values, start=1):
            val = val_info[0]
            align_h = val_info[1]
            custom_font = val_info[2]
            num_fmt = val_info[3] if len(val_info) > 3 else None

            cell = ws.cell(row=current_row, column=col_idx, value=val)
            cell.alignment = Alignment(horizontal=align_h, vertical="center")
            cell.border = cell_border

            if custom_font:
                cell.font = custom_font
            else:
                cell.font = Font(name="Arial", size=10, color="0F172A")

            # Qator foni
            cell.fill = PatternFill(start_color=row_bg, end_color=row_bg, fill_type="solid")

            # Raqam formati
            if num_fmt and isinstance(val, (int, float)):
                cell.number_format = num_fmt

            # Kechikish holati ustuni (col 7)
            if col_idx == 7:
                if is_ovd:
                    cell.fill = PatternFill(start_color=c_overdue_bg, end_color=c_overdue_bg, fill_type="solid")
                    cell.font = Font(name="Arial", size=10, bold=True, color=c_overdue_text)
                elif st_code == 'paid':
                    cell.font = Font(name="Arial", size=9, color="16A34A")

            # Holat ustuni (col 11)
            if col_idx == 11:
                if st_code == 'paid':
                    cell.fill = PatternFill(start_color=c_paid_bg, end_color=c_paid_bg, fill_type="solid")
                    cell.font = Font(name="Arial", size=10, bold=True, color=c_paid_text)
                elif st_code == 'partial':
                    cell.fill = PatternFill(start_color=c_partial_bg, end_color=c_partial_bg, fill_type="solid")
                    cell.font = Font(name="Arial", size=10, bold=True, color=c_partial_text)
                else:
                    cell.fill = PatternFill(start_color=c_unpaid_bg, end_color=c_unpaid_bg, fill_type="solid")
                    cell.font = Font(name="Arial", size=10, bold=True, color=c_unpaid_text)

        current_row += 1

    # 6. Jami qatori (Total Summary Row)
    ws.row_dimensions[current_row].height = 26
    c_total_bg = "E2E8F0"

    ws.merge_cells(start_row=current_row, start_column=1, end_row=current_row, end_column=8)
    total_label_cell = ws.cell(row=current_row, column=1)
    total_label_cell.value = f"JAMI: (O'quvchilar: {len(payments_data)} ta | To'langan: {paid_count} ta | Kechikkanlar: {overdue_count} ta)"
    total_label_cell.font = Font(name="Arial", size=10, bold=True, color="0F172A")
    total_label_cell.alignment = Alignment(horizontal="right", vertical="center")

    for c in range(1, 9):
        cell = ws.cell(row=current_row, column=c)
        cell.fill = PatternFill(start_color=c_total_bg, end_color=c_total_bg, fill_type="solid")
        cell.border = total_border

    # Jami tarif
    cell_tot_rate = ws.cell(row=current_row, column=9, value=total_rate)
    cell_tot_rate.font = Font(name="Arial", size=10, bold=True, color="0F172A")
    cell_tot_rate.alignment = Alignment(horizontal="right", vertical="center")
    cell_tot_rate.number_format = "#,##0"
    cell_tot_rate.fill = PatternFill(start_color=c_total_bg, end_color=c_total_bg, fill_type="solid")
    cell_tot_rate.border = total_border

    # Jami to'langan
    cell_tot_paid = ws.cell(row=current_row, column=10, value=total_paid)
    cell_tot_paid.font = Font(name="Arial", size=10, bold=True, color="15803D")
    cell_tot_paid.alignment = Alignment(horizontal="right", vertical="center")
    cell_tot_paid.number_format = "#,##0"
    cell_tot_paid.fill = PatternFill(start_color=c_total_bg, end_color=c_total_bg, fill_type="solid")
    cell_tot_paid.border = total_border

    for c in range(11, 14):
        cell = ws.cell(row=current_row, column=c, value="")
        cell.fill = PatternFill(start_color=c_total_bg, end_color=c_total_bg, fill_type="solid")
        cell.border = total_border

    # Ustun kengliklarini moslash
    for col_idx, (_, default_w, _) in enumerate(headers, start=1):
        col_letter = get_column_letter(col_idx)
        ws.column_dimensions[col_letter].width = default_w

    output = BytesIO()
    wb.save(output)
    output.seek(0)
    return output
