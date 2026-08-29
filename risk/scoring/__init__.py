"""As regras de priorização — o produto deste projeto.

Ficam em Python, não em SQL, de propósito. O diff de 500 mil linhas continua
no banco (é o que a spec §17.2 exige), mas a fórmula muda toda semana e
mantê-la em duas linguagens obrigaria a sincronizar cada ajuste de peso com uma
segunda cópia. O cálculo é stateless por linha e roda em segundos.
"""
