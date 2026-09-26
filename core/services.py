import io
import os
import uuid
from PIL import Image, ImageOps
from django.core.files.base import ContentFile
from django.http import HttpResponse
from .models import Group, Student

try:
    import pillow_heif
    pillow_heif.register_heif_opener()
except ImportError:
    pass

def optimize_student_photo(photo_file, max_size=(1200, 1200), quality=85):
    """
    Mobil telefonlardan (iOS / Android) olingan fotosuratlarni xavfsiz qayta ishlash:
    - HEIC / HEIF / JPEG / PNG / WEBP formatlarini qabul qiladi
    - EXIF orientation (aylanib qolish) muammosini tuzatadi
    - O'lchamini optimal (max 1200px) qilib siqadi
    - JPEG formatida ixcham va sifatli ContentFile qaytaradi
    """
    try:
        if hasattr(photo_file, 'seek'):
            photo_file.seek(0)
            
        img = Image.open(photo_file)
        
        # EXIF oriyentatsiyasini to'g'rilash (iPhone / Samsung vertikal rasmlari uchun)
        try:
            img = ImageOps.exif_transpose(img)
        except Exception:
            pass

        # Rang rejimini RGB ga o'tkazish
        if img.mode in ('RGBA', 'LA', 'P'):
            bg = Image.new('RGB', img.size, (255, 255, 255))
            if img.mode == 'P':
                img = img.convert('RGBA')
            mask = img.split()[-1] if 'A' in img.mode else None
            bg.paste(img, mask=mask)
            img = bg
        elif img.mode != 'RGB':
            img = img.convert('RGB')

        # O'lchamni thumbnail qilish
        img.thumbnail(max_size, Image.Resampling.LANCZOS)

        output = io.BytesIO()
        img.save(output, format='JPEG', quality=quality, optimize=True)
        output.seek(0)

        filename = f"{uuid.uuid4().hex[:12]}.jpg"
        return ContentFile(output.read(), name=filename), None
    except Exception as e:
        return None, str(e)


def generate_pdf_report():
    try:
        from reportlab.lib.pagesizes import letter
        from reportlab.pdfgen import canvas
        from reportlab.lib import colors
        from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
        from reportlab.lib.styles import getSampleStyleSheet
    except ImportError:
        return HttpResponse("ReportLab kutubxonasi o'rnatilmagan (pip install reportlab)", status=500)

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter)
    elements = []
    
    styles = getSampleStyleSheet()
    elements.append(Paragraph("O'quv Markazi - Guruhlar va O'quvchilar Hisoboti", styles['Title']))
    elements.append(Spacer(1, 12))
    
    groups = Group.objects.all()
    
    for group in groups:
        elements.append(Paragraph(f"Guruh: {group.name} (O'qituvchi: {group.teacher})", styles['Heading2']))
        
        # Collect students for all timeslots in this group
        data = [["O'quvchi F.I.O.", "Yoshi", "Telefon 1", "Qo'shilgan sana", "Status"]]
        for ts in group.time_slots.all():
            for student in ts.students.filter(is_active=True):
                data.append([
                    f"{student.first_name} {student.last_name}",
                    str(student.age),
                    student.phone_1,
                    student.joined_date.strftime('%Y-%m-%d'),
                    "Faol"
                ])
        
        if len(data) > 1:
            table = Table(data)
            table.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, 0), colors.grey),
                ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
                ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
                ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                ('BOTTOMPADDING', (0, 0), (-1, 0), 12),
                ('BACKGROUND', (0, 1), (-1, -1), colors.beige),
                ('GRID', (0, 0), (-1, -1), 1, colors.black),
            ]))
            elements.append(table)
        else:
            elements.append(Paragraph("Bu guruhda o'quvchilar yo'q.", styles['Normal']))
            
        elements.append(Spacer(1, 24))
        
    doc.build(elements)
    buffer.seek(0)
    
    response = HttpResponse(buffer, content_type='application/pdf')
    response['Content-Disposition'] = 'attachment; filename="hisobot.pdf"'
    return response
