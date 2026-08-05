from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

c = canvas.Canvas("plano_sintetico.pdf", pagesize=letter)
width, height = letter

# Marcas de zona en el borde (deben ser IGNORADAS por el filtro)
zone_labels = ["4", "3", "2", "1"]
for i, label in enumerate(zone_labels):
    x = 80 + i * 150
    c.drawString(x, height - 20, label)
    c.drawString(x, 15, label)

# Cajetin / titulo (debe ser ignorado)
c.setFont("Helvetica", 8)
c.drawString(450, 40, "DRAWN")
c.drawString(450, 30, "Juan Perez")
c.drawString(450, 20, "SCALE: 1:1")
c.drawString(550, 40, "REV")
c.drawString(550, 30, "A")

# Cotas reales (30 en total), varios formatos
c.setFont("Helvetica", 10)
dimensions = [
    "10.00", "5.5", ".75", ".250", "2.500\u00b1.002", "R.25 TYP", "R5",
    "\u00d8.500 THRU", "\u00d810", "1/4-20 UNC - 2B", "4x \u00d83", "6", "12.5",
    "8.0", "3.1", "45.5", "46", "22.5", "13.5", "15.5", "23.2", "44.5",
    "52.5", "25", "21", "7.5", "58.5", "47.5", "85", "9.5",
]

x, y = 60, height - 100
for i, dim in enumerate(dimensions):
    c.drawString(x, y, dim)
    y -= 22
    if y < 60:
        y = height - 100
        x += 140

c.save()
print("PDF generado: plano_sintetico.pdf")