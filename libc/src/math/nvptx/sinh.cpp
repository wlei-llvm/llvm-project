//===-- Implementation of the sinh function for GPU -----------------------===//
//
// Part of the LLVM Project, under the Apache License v2.0 with LLVM Exceptions.
// See https://llvm.org/LICENSE.txt for license information.
// SPDX-License-Identifier: Apache-2.0 WITH LLVM-exception
//
//===----------------------------------------------------------------------===//

#include "src/math/sinh.h"
#include "src/__support/common.h"

#include "declarations.h"
#include "src/__support/macros/config.h"

namespace LIBC_NAMESPACE_DECL {

LLVM_LIBC_FUNCTION(double, sinh, (double x)) { return __nv_sinh(x); }

} // namespace LIBC_NAMESPACE_DECL
