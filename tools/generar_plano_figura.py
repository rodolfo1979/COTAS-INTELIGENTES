from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

c = canvas.Canvas("plano_figura.pdf", pagesize=letter)
width, height = letter

def dim_line_h(c, x1, x2, y, text, tick=6, offset_text=4):
    """Linea de cota horizontal con flechas y texto centrado arriba."""
    c.line(x1, y, x2, y)
    c.line(x1, y - tick, x1, y + tick)
    c.line(x2, y - tick, x2, y + tick)
    c.setFont("Helvetica", 9)
    c.drawCentredString((x1 + x2) / 2, y + offset_text, text)

def dim_line_v(c, x, y1, y2, text, tick=6, offset_text=4):
    """Linea de cota vertical con flechas y texto rotado al lado."""
    c.line(x, y1, x, y2)
    c.line(x - tick, y1, x + tick, y1)
    c.line(x - tick, y2, x + tick, y2)
    c.setFont("Helvetica", 9)
    c.saveState()
    c.translate(x - offset_text - 8, (y1 + y2) / 2)
    c.rotate(90)
    c.drawCentredString(0, 0, text)
    c.restoreState()

def ext_line(c, x1, y1, x2, y2):
    """Linea de extension (delgada) desde el contorno hasta la linea de cota."""
    c.setLineWidth(0.4)
    c.line(x1, y1, x2, y2)
    c.setLineWidth(1)

# --- Marcas de zona en el borde (deben ser IGNORADAS) ---
c.setFont("Helvetica", 9)
for i, label in enumerate(["4", "3", "2", "1"]):
    x = 80 + i * 150
    c.drawString(x, height - 20, label)
    c.drawString(x, 15, label)

# --- Cajetin (debe ser IGNORADO) ---
c.setFont("Helvetica", 8)
c.drawString(450, 40, "DRAWN")
c.drawString(450, 30, "Juan Perez")
c.drawString(450, 20, "SCALE: 1:1")
c.drawString(550, 40, "REV")
c.drawString(550, 30, "A")

# --- Figura principal: pieza rectangular con un agujero ---
px0, py0, px1, py1 = 120, 250, 480, 550
c.rect(px0, py0, px1 - px0, py1 - py0)

hole_cx, hole_cy, hole_r = 300, 400, 25
c.circle(hole_cx, hole_cy, hole_r)

notch_x0, notch_y0, notch_x1, notch_y1 = 420, 250, 480, 300
c.line(notch_x0, notch_y1, notch_x1, notch_y1)
c.line(notch_x0, notch_y0, notch_x0, notch_y1)

# --- Cotas alrededor de la figura ---

# Ancho total (abajo)
ext_line(c, px0, py0, px0, py0 - 40)
ext_line(c, px1, py0, px1, py0 - 40)
dim_line_h(c, px0, px1, py0 - 25, "44.5")

# Alto total (izquierda)
ext_line(c, px0, py0, px0 - 40, py0)
ext_line(c, px0, py1, px0 - 40, py1)
dim_line_v(c, px0 - 25, py0, py1, "58.5")

# Distancia del borde izquierdo al centro del agujero
ext_line(c, px0, hole_cy, px0 - 70, hole_cy)
ext_line(c, hole_cx, hole_cy, hole_cx, py1 + 40)
dim_line_h(c, px0, hole_cx, py1 + 25, "21")

# Diametro del agujero (etiqueta con globo simulado, sin linea)
c.setFont("Helvetica", 9)
c.drawString(hole_cx + hole_r + 10, hole_cy + hole_r + 5, "\u00d850")

# Radio de una esquina (simulado con nota + flecha corta)
c.line(px1 - 15, py1 - 15, px1 - 40, py1 - 40)
c.drawString(px1 - 60, py1 - 55, "R5")

# Muesca (notch) - dos cotas chicas
ext_line(c, notch_x0, py0, notch_x0, py0 - 70)
dim_line_h(c, notch_x0, px1, py0 - 55, "7.5")
ext_line(c, notch_x1, notch_y1, notch_x1 + 30, notch_y1)
dim_line_v(c, notch_x1 + 15, notch_y0, notch_y1, "6")

# Tolerancia compuesta cerca del agujero
c.drawString(hole_cx - 40, hole_cy - hole_r - 20, "2.500\u00b1.002")

# Cotas sueltas alrededor (simulando anotaciones tipicas)
extra_dims = [
    ".75", "R.25 TYP", "1/4-20 UNC - 2B", "4x \u00d83", "12.5",
    "8.0", "3.1", "45.5", "46", "22.5", "13.5", "15.5", "23.2",
    "52.5", "25", "9.5", "10.00", "5.5",
]
x, y = 60, 700
for i, dim in enumerate(extra_dims):
    c.setFont("Helvetica", 9)
    c.drawString(x, y, dim)
    y -= 16
    if y < 560:
        y = 700
        x += 130

c.save()
print("PDF generado: plano_figura.pdf")