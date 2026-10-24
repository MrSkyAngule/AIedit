"""Иконки боковой полосы, нарисованные кодом: без файлов-картинок и модуля SVG, под цвет темы."""
from .qt import QApplication, QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap, Qt

SIZE = 64  # рисуем крупно, Qt уменьшит под кнопку


def _color() -> QColor:
    app = QApplication.instance()
    return app.palette().windowText().color() if app else QColor("#444")


def _paint(draw) -> QIcon:
    pix = QPixmap(SIZE, SIZE)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(_color(), 4.5)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    draw(p)
    p.end()
    return QIcon(pix)


def files() -> QIcon:
    """Два листа: проводник по файлам."""
    def draw(p):
        back = QPainterPath()
        back.moveTo(22, 14)
        back.lineTo(22, 8)
        back.lineTo(44, 8)
        back.lineTo(54, 18)
        back.lineTo(54, 46)
        back.lineTo(46, 46)
        p.drawPath(back)
        front = QPainterPath()
        front.moveTo(10, 18)
        front.lineTo(32, 18)
        front.lineTo(42, 28)
        front.lineTo(42, 56)
        front.lineTo(10, 56)
        front.closeSubpath()
        p.drawPath(front)
    return _paint(draw)


def git() -> QIcon:
    """Ветка: два коммита на основной линии и один на боковой."""
    def draw(p):
        p.drawLine(18, 17, 18, 47)
        p.drawEllipse(12, 6, 12, 12)
        p.drawEllipse(12, 46, 12, 12)
        p.drawEllipse(40, 14, 12, 12)
        branch = QPainterPath()
        branch.moveTo(46, 26)
        branch.cubicTo(46, 38, 20, 34, 18, 44)
        p.drawPath(branch)
    return _paint(draw)


def chat() -> QIcon:
    """Облачко с хвостиком: чат с ИИ."""
    def draw(p):
        bubble = QPainterPath()
        bubble.moveTo(16, 10)
        bubble.lineTo(48, 10)
        bubble.quadTo(56, 10, 56, 18)
        bubble.lineTo(56, 36)
        bubble.quadTo(56, 44, 48, 44)
        bubble.lineTo(30, 44)
        bubble.lineTo(18, 54)
        bubble.lineTo(20, 44)
        bubble.lineTo(16, 44)
        bubble.quadTo(8, 44, 8, 36)
        bubble.lineTo(8, 18)
        bubble.quadTo(8, 10, 16, 10)
        p.drawPath(bubble)
        for x in (21, 32, 43):
            p.drawPoint(x, 27)
    return _paint(draw)
