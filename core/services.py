import io
from django.http import HttpResponse
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet
from .models import Group, Student

def generate_pdf_report():
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
