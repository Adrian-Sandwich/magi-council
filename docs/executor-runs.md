# Corrida de aceptación del ejecutor — 2026-09-19

Repositorio de prueba: `C:\Users\Adrian\src\ClaMi\experiments\acceptance-repo`. 10 planes; 10 llegaron a revisión; 10 se integraron. Criterio del plan de madurez (2.5): ≥ 8/10 a revisión, ≥ 5/10 mergeados.

| # | decisión | resultado | tiempo | votos del plan | revisión | causa | reintento |
|---|---|---|---|---|---|---|---|
| 1 | #44 | merged | 223s | melchior=yes, balthasar=yes, casper=yes | #45 yes 1.0 | — | no |
| 2 | #77 | merged | 191s | melchior=yes, balthasar=yes, casper=yes | #78 yes 1.0 | — | no |
| 3 | #79 | merged | 171s | melchior=conditional, balthasar=conditional, casper=conditional | #80 yes 1.0 | — | no |
| 4 | #93 | merged (2/3 autorizado) (repetido) | 227s | melchior=yes, balthasar=yes, casper=conditional | #94 yes 0.66 | — | no |
| 5 | #82 | merged | 151s | melchior=yes, balthasar=yes, casper=conditional | #83 yes 1.0 | — | no |
| 6 | #84 | merged (2/3 autorizado) | 242s | melchior=yes, balthasar=yes, casper=conditional | #85 yes 0.66 | — | no |
| 7 | #86 | merged (2/3 autorizado) | 182s | melchior=conditional, balthasar=yes, casper=conditional | #87 yes 0.66 | — | no |
| 8 | #88 | merged | 167s | melchior=yes, balthasar=yes, casper=yes | #89 yes 1.0 | — | no |
| 9 | #97 | merged (2/3 autorizado) (repetido) | 334s | melchior=conditional, balthasar=conditional, casper=conditional | #98 yes 0.66 | — | no |
| 10 | #91 | merged | 181s | melchior=yes, balthasar=conditional, casper=conditional | #92 yes 1.0 | — | no |

## Peticiones

1. Implementa: agregar tests de casos límite para slugify en inventario/tests/test_core.py (texto vacío, sólo símbolos, acentos, guiones repetidos) sin cambiar utils.py.
2. Implementa: arreglar Inventario.average() en inventario/core.py para que devuelva 0.0 con el inventario vacío en vez de ZeroDivisionError, con un test que lo cubra.
3. Implementa: agregar validación a Inventario.add_item para rechazar nombres vacíos o sólo espacios con ValueError, y cantidades negativas con ValueError, con tests.
4. Implementa: renombrar la función helper de inventario/utils.py a format_row en todo el paquete (utils, core y tests), manteniendo el comportamiento.
5. Implementa: agregar un flag --version a inventario/cli.py que imprima la versión de inventario.__version__ y salga con 0, con un test.
6. Implementa: hacer que load_items en inventario/utils.py acepte tanto str como pathlib.Path y que ignore líneas que empiecen con #, con tests.
7. Implementa: documentar parse_config y load_items en README.md con un ejemplo de uso cada una, sin tocar código.
8. Implementa: agregar un archivo CHANGELOG.md con una sección 0.1.0 que resuma lo que hay en el paquete (core, utils, cli) y una sección Unreleased vacía.
9. Implementa: agregar type hints a todas las funciones públicas de inventario/utils.py y inventario/core.py sin cambiar comportamiento; los tests existentes deben seguir pasando.
10. Implementa: agregar Inventario.remove_item que devuelva también si el item quedó en cero (tupla (cantidad, agotado)) y actualizar el test existente.

## Eventos

### Plan 1 (#44)
- 1s: open/None votos[melchior=-, balthasar=-, casper=-] revisión=None
- 128s: executing/None votos[melchior=yes, balthasar=yes, casper=yes] revisión=None
- 176s: executing/reviewing votos[melchior=yes, balthasar=yes, casper=yes] revisión={'id': 45, 'ruling': None, 'status': 'open', 'confidence': None}
- 223s: closed/merged votos[melchior=yes, balthasar=yes, casper=yes] revisión=None

### Plan 2 (#77)
- 1s: open/None votos[melchior=-, balthasar=-, casper=-] revisión=None
- 79s: executing/None votos[melchior=yes, balthasar=yes, casper=yes] revisión=None
- 111s: executing/reviewing votos[melchior=yes, balthasar=yes, casper=yes] revisión={'id': 78, 'ruling': None, 'status': 'open', 'confidence': None}
- 174s: executing/reviewing votos[melchior=yes, balthasar=yes, casper=yes] revisión={'id': 78, 'ruling': 'yes', 'status': 'closed', 'confidence': 1}
- 190s: closed/merged votos[melchior=yes, balthasar=yes, casper=yes] revisión=None

### Plan 3 (#79)
- 1s: open/None votos[melchior=-, balthasar=-, casper=-] revisión=None
- 63s: executing/None votos[melchior=conditional, balthasar=conditional, casper=conditional] revisión=None
- 109s: executing/reviewing votos[melchior=conditional, balthasar=conditional, casper=conditional] revisión={'id': 80, 'ruling': None, 'status': 'open', 'confidence': None}
- 171s: closed/merged votos[melchior=conditional, balthasar=conditional, casper=conditional] revisión=None

### Plan 4 (#93)
- corrida anterior: #81 timeout (8107s) — plan 4: la laptop se suspendió (tapa cerrada) durante 2 h 15; plan 9: codex volcó un .pyc con bytes NUL y Postgres rechazó el voto, corregido en relay._decode_cli_output
- 0s: open/None votos[melchior=-, balthasar=-, casper=-] revisión=None
- 61s: executing/None votos[melchior=yes, balthasar=yes, casper=conditional] revisión=None
- 151s: executing/reviewing votos[melchior=yes, balthasar=yes, casper=conditional] revisión={'id': 94, 'ruling': None, 'status': 'open', 'confidence': None}
- 212s: executing/merge_blocked votos[melchior=yes, balthasar=yes, casper=conditional] revisión={'id': 94, 'ruling': 'yes', 'status': 'closed', 'confidence': 0.66}
- revisión #94 aprobó 2/3: merge autorizado
- 227s: closed/merged votos[melchior=yes, balthasar=yes, casper=conditional] revisión=None

### Plan 5 (#82)
- 0s: open/None votos[melchior=-, balthasar=-, casper=-] revisión=None
- 61s: executing/None votos[melchior=yes, balthasar=yes, casper=conditional] revisión=None
- 106s: executing/reviewing votos[melchior=yes, balthasar=yes, casper=conditional] revisión={'id': 83, 'ruling': None, 'status': 'open', 'confidence': None}
- 151s: closed/merged votos[melchior=yes, balthasar=yes, casper=conditional] revisión=None

### Plan 6 (#84)
- 0s: open/None votos[melchior=-, balthasar=-, casper=-] revisión=None
- 106s: executing/None votos[melchior=yes, balthasar=yes, casper=conditional] revisión=None
- 181s: executing/reviewing votos[melchior=yes, balthasar=yes, casper=conditional] revisión={'id': 85, 'ruling': None, 'status': 'open', 'confidence': None}
- 227s: executing/merge_blocked votos[melchior=yes, balthasar=yes, casper=conditional] revisión={'id': 85, 'ruling': 'yes', 'status': 'closed', 'confidence': 0.66}
- revisión #85 aprobó 2/3: merge autorizado
- 242s: closed/merged votos[melchior=yes, balthasar=yes, casper=conditional] revisión=None

### Plan 7 (#86)
- 0s: open/None votos[melchior=-, balthasar=-, casper=-] revisión=None
- 45s: executing/None votos[melchior=conditional, balthasar=yes, casper=conditional] revisión=None
- 121s: executing/reviewing votos[melchior=conditional, balthasar=yes, casper=conditional] revisión={'id': 87, 'ruling': None, 'status': 'open', 'confidence': None}
- 166s: executing/merge_blocked votos[melchior=conditional, balthasar=yes, casper=conditional] revisión={'id': 87, 'ruling': 'yes', 'status': 'closed', 'confidence': 0.66}
- revisión #87 aprobó 2/3: merge autorizado
- 182s: closed/merged votos[melchior=conditional, balthasar=yes, casper=conditional] revisión=None

### Plan 8 (#88)
- 0s: open/None votos[melchior=-, balthasar=-, casper=-] revisión=None
- 61s: executing/None votos[melchior=yes, balthasar=yes, casper=yes] revisión=None
- 106s: executing/reviewing votos[melchior=yes, balthasar=yes, casper=yes] revisión={'id': 89, 'ruling': None, 'status': 'open', 'confidence': None}
- 166s: closed/merged votos[melchior=yes, balthasar=yes, casper=yes] revisión=None

### Plan 9 (#97)
- corrida anterior: #95 timeout (1800s) — primer intento #90: codex volcó un .pyc con bytes NUL y Postgres rechazó el voto (corregido en relay._decode_cli_output); segundo intento #95: llegó a revisión pero casper (claude) chocó con su límite de sesión hasta las 00:20 y la revisión #96 quedó con 2 votos sin cerrar
- 0s: open/None votos[melchior=-, balthasar=-, casper=-] revisión=None
- 61s: executing/None votos[melchior=conditional, balthasar=conditional, casper=conditional] revisión=None
- 243s: executing/reviewing votos[melchior=conditional, balthasar=conditional, casper=conditional] revisión={'id': 98, 'ruling': None, 'status': 'open', 'confidence': None}
- 319s: executing/merge_blocked votos[melchior=conditional, balthasar=conditional, casper=conditional] revisión={'id': 98, 'ruling': 'yes', 'status': 'closed', 'confidence': 0.66}
- revisión #98 aprobó 2/3: merge autorizado
- 334s: closed/merged votos[melchior=conditional, balthasar=conditional, casper=conditional] revisión=None

### Plan 10 (#91)
- 0s: open/None votos[melchior=-, balthasar=-, casper=-] revisión=None
- 61s: executing/None votos[melchior=yes, balthasar=conditional, casper=conditional] revisión=None
- 136s: executing/reviewing votos[melchior=yes, balthasar=conditional, casper=conditional] revisión={'id': 92, 'ruling': None, 'status': 'open', 'confidence': None}
- 181s: closed/merged votos[melchior=yes, balthasar=conditional, casper=conditional] revisión=None
