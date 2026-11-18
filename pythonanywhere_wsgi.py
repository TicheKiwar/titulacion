# Contenido para el archivo de configuración WSGI de PythonAnywhere
# (pestaña Web → "WSGI configuration file"). Reemplaza TODO su contenido por esto
# y cambia TU_USUARIO por tu nombre de usuario de PythonAnywhere.
import sys

ruta = "/home/TU_USUARIO/titulacion"
if ruta not in sys.path:
    sys.path.insert(0, ruta)

from app import application  # noqa: E402,F401
