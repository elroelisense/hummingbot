# Escalera B — PREPARADA, NO ACTIVAR hasta el martes

> Estado: planificada. No colocar ordenes hasta el martes a la noche,
> cuando recalculemos con el precio real de ese dia. Compatible con
> v6, v6.1 o v7 (usa un tramo de PEPE separado, no toca su colateral).

## Que es
4 ordenes LIMIT GTC de VENTA en Binance spot PEPE-USDT, colocadas
una sola vez. Capturan los picos explosivos de PEPE que el PMM/grid
no puede (el PMM vende a +0.6%, la escalera vende a +15%/+100%).
Intervencion humana: cero hasta que se llenen.

## Tramo apartado (no compite con el bot activo)
- Bag Binance: 41,975,171 PEPE
- Tramo escalera: 8,000,000 PEPE (~19% del bag, ~$39.60 al mid actual)
- Colateral PMM/grid: ~3,100,000 PEPE bloqueados por el bot
- PEPE libre restante: ~30,800,000 (colchon de sobra)
- La escalera NO toca USDT: solo consume PEPE del bag.

## Niveles (referencia mid 0.00000495 del 08-nov; RECALCULAR el martes)
| Nivel | % vs mid | Precio | PEPE | Nocional si llena |
|-------|----------|--------|------|-------------------|
| 1 | +15%  | 0.00000569 | 2,000,000 | $11.38 |
| 2 | +30%  | 0.00000644 | 2,000,000 | $12.87 |
| 3 | +60%  | 0.00000792 | 2,000,000 | $15.84 |
| 4 | +100% | 0.00000990 | 2,000,000 | $19.80 |
| Total | | | 8,000,000 | $59.89 si llenan todos |

Si llenan los 4 niveles: realizas $59.89 desde un tramo que hoy vale
$39.60 = **+$20.29** vs. mantener ese PEPE quieto.
Si no llena ninguno: costo $0. Las ordenes descansan en el libro.

## Como colocarla el martes (en Binance UI, 2 minutos)
1. Spot > PEPE/USDT > Limit > GTC (post-only si esta disponible).
2. Una orden por nivel con precio y cantidad de la tabla,
   RECALCULADOS con el mid real del martes:
   precio_nivel = mid_martes * (1 + %)
   (ej. si el martes mid = 0.00000520: niveles 0.00000598 / 0.00000676 /
   0.00000832 / 0.00001040, misma cantidad 2,000,000 PEPE cada uno.)
3. Verifica en "Ordenes abiertas" que las 4 figuren.
4. No colocar montos mayores: el tramo es fijo en 8M PEPE.
   Si el bot activo cambia (v6.1 o v7), la escalera no se toca.

## Compatibilidad
- Con v6 / v6.1 (PMM): sin conflicto. PMM vende a mid+0.6% (renueva
  cada 120-180s), escalera a +15% o mas (quieta en el libro).
- Con v7 (grid SELL): sin conflicto. Grid vende dentro de +-15%,
  escalera por encima del +15%.
- La escalera NO necesita Hummingbot: son ordenes GTC del exchange.
