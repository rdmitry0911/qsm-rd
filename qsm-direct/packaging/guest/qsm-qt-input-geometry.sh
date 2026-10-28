# SPDX-License-Identifier: AGPL-3.0-or-later
# Sourced by Plasma before desktop applications are started.
for _qsm_qt_geometry_library in /usr/lib/*/qt[56]/plugins/generic/libqsm-input-geometry.so; do
    [ -f "$_qsm_qt_geometry_library" ] || continue
    case ",${QT_QPA_GENERIC_PLUGINS-}," in
        *,qsm-input-geometry,*|*,qsm-input-geometry:*) ;;
        *)
            QT_QPA_GENERIC_PLUGINS="${QT_QPA_GENERIC_PLUGINS:+${QT_QPA_GENERIC_PLUGINS},}qsm-input-geometry"
            export QT_QPA_GENERIC_PLUGINS
            ;;
    esac
    break
done
unset _qsm_qt_geometry_library
