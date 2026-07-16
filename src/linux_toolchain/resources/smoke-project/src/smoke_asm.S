/*
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

    .text
    .globl linux_toolchain_smoke_asm
    .type linux_toolchain_smoke_asm, %function
linux_toolchain_smoke_asm:
#if defined(__x86_64__)
    mov $17, %eax
    ret
#elif defined(__aarch64__)
    mov w0, #17
    ret
#else
#error "unsupported smoke-project architecture"
#endif
    .size linux_toolchain_smoke_asm, .-linux_toolchain_smoke_asm

#if defined(__x86_64__)
    .section .note.GNU-stack,"",@progbits
#else
    .section .note.GNU-stack,"",%progbits
#endif
