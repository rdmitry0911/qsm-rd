#pragma once

#include "interfaces/media_adapter.hpp"

namespace qmdp {

// Compatibility spelling for existing native GameStream integration code.
// New transports must depend on IMediaAdapter directly.
using ISunshineAdapter = IMediaAdapter;

}  // namespace qmdp
