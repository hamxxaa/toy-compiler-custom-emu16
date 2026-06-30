import os
from codegen.TACGenerator import Const, TempVar, Var

from .emu_isa import (
    OP_NOP,
    OP_ADD,
    OP_AND,
    OP_CAL,
    OP_CMP,
    OP_HLT,
    OP_JC,
    OP_JNC,
    OP_JMP,
    OP_JNZ,
    OP_JNS,
    OP_JS,
    OP_JZ,
    OP_LDI,
    OP_LDROFF,
    OP_LDR,
    OP_MOV,
    OP_MUL,
    OP_DIV,
    OP_OR,
    OP_POP,
    OP_PSH,
    OP_RET,
    OP_SHL,
    OP_SHR,
    OP_STR,
    OP_STRI,
    OP_STROFF,
    OP_SUB,
    OP_XOR,
    pack_i16_header,
    pack_r_type,
    pack_m_type,
    pack_m_type_abs,
    ABS_FLAG,
)
from .core.liveness import analyze_liveness
from .core.allocator import allocate_function, LocationMap
from .core.function_frame import FunctionFrame


class EmuBackend:
    # Memory layout:
    #   0x0000-0x0007  Bootstrap (LDI sp + JMP code_start)
    #   0x0008-0x3FFF  Data section (globals + arrays incl. the ROM-shipped font, AND streamed
    #                  sprite sheets — the sprite pool lives here; 16 KB as of the sprite library)
    #   0x4000-0xADFB  Code section (functions, grows up from 0x4000)
    #   0xADFC         Stack base (grows DOWN from here)
    #   0xADFE         SYSCALL_PORT (write-triggered host call)
    #   0xADFF         INPUT (hardware mapped)
    #   0xAE00-0xAFFF  PRAM (hardware mapped, 512 bytes)
    #   0xB000-0xFFFF  VRAM (hardware mapped, 20480 bytes)
    # NOTE: the DATA/CODE boundary lives only in the ROM bootstrap's `JMP CODE_START`; the
    # emulators load the flat image and execute it, so moving it is a compiler-only change.
    CODE_START_ADDRESS = 0x4000
    DATA_START_ADDRESS = 0x0008
    DATA_END_ADDRESS   = 0x4000   # data grows up to code start (16 KB for globals + streamed sheets)
    STACK_START_ADDRESS = 0xADFC
    SYSCALL_PORT = 0xADFE
    INPUT_ADDRESS = 0xADFF

    # Calling convention registers
    # R0: return value
    # R1-R3: first 3 arguments (caller-saved)
    # R4-R5: callee-saved registers
    # R6: frame pointer (reserved)
    # R7: stack pointer (reserved)
    REG_RETVAL = 0           # Return value
    REG_ARG1 = 1             # First argument
    REG_ARG2 = 2             # Second argument
    REG_ARG3 = 3             # Third argument (can be used as temp for addressing)
    REG_SAVED1 = 4           # Callee-saved register 1
    REG_SAVED2 = 5           # Callee-saved register 2
    REG_FP = 6               # Frame pointer (reserved)
    REG_SP = 7               # Stack pointer (reserved)
    
    # Aliases for backward compatibility
    REG_A = REG_ARG1
    REG_B = REG_ARG2
    REG_ADDR = REG_ARG3      # Use R3 as temp for address calculation (when needed)

    # Binary-op opcode map + which ops are commutative (operand-aware emission).
    ALU_OPCODE = {
        "+": OP_ADD, "-": OP_SUB, "&": OP_AND, "|": OP_OR, "^": OP_XOR,
        "*": OP_MUL, "/": OP_DIV, "shl": OP_SHL, "shr": OP_SHR,
    }
    COMMUTATIVE = {"+", "&", "|", "^", "*"}

    def __init__(self, tac, address_taken=None, print_alloc=False, print_emit=False):
        self.tac = tac
        self.address_taken = address_taken or set()
        self.code = bytearray()
        self.labels = {}
        self.fixups = []
        self.temp_label_counter = 0
        self.operand_addresses = {}
        self.next_data_address = self.DATA_START_ADDRESS
        # Array-literal initializers to bake into the ROM image's DATA region: (address, bytes).
        self.data_initializers = []
        
        # Function context for prologue/epilogue and allocation
        self.current_function = None
        # Maps function name -> FunctionFrame
        self.function_frames = {}
        
        # Argument passing buffer: collects arguments until call instruction
        self.pending_args = []  # List of operands to be passed as arguments
        
        # Current instruction index within a function
        self.current_instr_index = 0
        self.emit_debug = print_emit
        self.print_alloc = print_alloc

        # Phase-2 optimization marks, precomputed in the pre-pass and keyed by id(instr)
        # (id, not current_instr_index, because that index doesn't advance on param/label/arg).
        self.fused_cmp = {}     # id(comparison) -> target label: emit CMP + direct branch
        self.skip_if = set()    # id(if): already handled by a fused comparison, emit nothing
        self.call_spill = {}    # id(call) -> [caller-saved regs live across the call, to spill]
        self.dead_instr = set() # id(instr): dead pure-def, skip emission (DSE)


    def _analyze_and_allocate_functions(self):
        """Pre-pass: analyze each function and create FunctionFrames."""
        current_func = None
        func_instructions = {}
        func_params = {}
        
        for instr in self.tac.instructions:
            if instr.op == "func_begin":
                current_func = instr.arg1
                func_instructions[current_func] = []
                func_params[current_func] = []
            elif instr.op == "func_end":
                current_func = None
            elif current_func:
                func_instructions[current_func].append(instr)
                if instr.op == "param":
                    func_params[current_func].append(instr.arg1)
                    
        for func_name, instrs in func_instructions.items():
            if not instrs:
                continue

            # Naked function: body is a single asm block (ignoring param markers).
            body = [i for i in instrs if i.op != "param"]
            if len(body) == 1 and body[0].op == "asm":
                self.function_frames[func_name] = FunctionFrame(
                    name=func_name,
                    param_count=len(func_params[func_name]),
                    param_names=func_params[func_name],
                    location_map=LocationMap({}, [], 0),
                    liveness=None,
                    callee_saved=[],
                    frame_size=0,
                    epilogue_label="",
                    is_naked=True,
                )
                continue

            liveness = analyze_liveness(instrs)
            param_names = func_params[func_name]
            
            # The epilogue label name
            epilogue_label = f".epilogue_{func_name}"
            
            loc_map = allocate_function(liveness, len(param_names), param_names,
                                        self.address_taken, verbose=self.print_alloc)
            
            frame = FunctionFrame(
                name=func_name,
                param_count=len(param_names),
                param_names=param_names,
                location_map=loc_map,
                liveness=liveness,
                callee_saved=loc_map.callee_saved,
                frame_size=loc_map.frame_size,
                epilogue_label=epilogue_label
            )
            self.function_frames[func_name] = frame
            self._precompute_marks(instrs, liveness, loc_map)

    # Comparison ops that set flags and can drive a conditional branch directly.
    _CMP_OPS = {"<", "<=", ">", ">=", "==", "!="}
    # Pure, side-effect-free ops whose result is a value — candidates for dead-store elimination.
    # Deliberately excludes call / store_ptr / load_ptr / def_arr (effects, or load may touch MMIO).
    _DSE_OPS = {"+", "-", "*", "/", "|", "&", "^", "shl", "shr",
                "<", "<=", ">", ">=", "==", "!=", "def", "eq", "addrof"}

    def _is_addr_taken(self, var):
        """True if `var`'s address is taken anywhere (&var). Mirrors allocator._is_addr_taken:
        the address_taken set is keyed by (base_name, scope_id)."""
        if not self.address_taken or not hasattr(var, "name") or not hasattr(var, "scope_id"):
            return False
        base = var.name.split("_s")[0] if "_s" in var.name else var.name
        return (base, var.scope_id) in self.address_taken

    def _precompute_marks(self, instrs, liveness, loc_map):
        """Populate the phase-2 optimization marks (fused_cmp / skip_if / call_spill / dead_instr)
        for one function from its liveness + allocation. Keyed by id(instr)."""
        live_at = liveness.live_at
        n = len(instrs)
        reg_of = {v: loc.register for v, loc in loc_map.locations.items()
                  if loc.kind == "register"}

        for i, ins in enumerate(instrs):
            # --- Compare+branch fusion: `cmp -> if` adjacent, result used only by that if ---
            if ins.op in self._CMP_OPS and i + 1 < n:
                nxt = instrs[i + 1]
                dead_after = (i + 2 > n) or (ins.result not in live_at[i + 2])
                if nxt.op == "if" and nxt.arg1 is ins.result and dead_after:
                    self.fused_cmp[id(ins)] = nxt.result   # branch target label
                    self.skip_if.add(id(nxt))

            # --- Call-site spilling: only caller-saved regs live across the call ---
            if ins.op == "call":
                live_after = live_at[i + 1] if i + 1 < len(live_at) else set()
                self.call_spill[id(ins)] = [r for r in (self.REG_ARG1, self.REG_ARG2, self.REG_ARG3)
                                            if any(reg_of.get(v) == r for v in live_after)]

            # --- Dead-store elimination: pure def whose result is never read again ---
            if ins.op in self._DSE_OPS and isinstance(ins.result, (Var, TempVar)):
                if ins.op in ("def", "eq") and ins.arg1 is None:
                    continue   # bare declaration — already emits nothing
                # Per-function liveness can't observe two kinds of cross-context reads, so a store
                # to either is NEVER provably dead here:
                #   - globals: another function may read them.
                #   - address-taken locals: a later *p read goes through the pointer, not the name.
                if getattr(ins.result, "storage", None) == "global":
                    continue
                if self._is_addr_taken(ins.result):
                    continue
                if i + 1 <= n and ins.result not in (live_at[i + 1] if i + 1 < len(live_at) else set()):
                    self.dead_instr.add(id(ins))

    def generate(self):
        # Pre-analysis pass: do liveness analysis and register allocation for each function
        # Must be done before emission so we know register assignments and spill slots
        self._analyze_and_allocate_functions()
        
        self._emit_bootstrap_jump()
        self._pad_to_code_start()

        for instruction in self.tac.instructions:
            self._emit_instruction(instruction)

        # Ensure deterministic termination even if TAC does not end with ret.
        self._emit_i16(OP_HLT, 0)

        self._resolve_fixups()
        self._bake_data_initializers()
        return bytes(self.code)

    def _bake_data_initializers(self):
        """Write array-literal bytes into the ROM image at their DATA addresses. The 0x0008-0x0FFF
        region is already present in self.code as zero-padding (from _pad_to_code_start), so this
        only overwrites padding and never grows the image. Loaded verbatim by every host, so the
        data is present in memory the moment the ROM is loaded — no host-side initialization."""
        for addr, byts in self.data_initializers:
            for i, b in enumerate(byts):
                self.code[addr + i] = b & 0xFF

    def _emit_bootstrap_jump(self):
        # Initialize stack pointer before jumping to code
        self._emit_i16(OP_LDI, self.STACK_START_ADDRESS, reg=self.REG_SP)
        self._emit_i16(OP_JMP, self.CODE_START_ADDRESS)

    def _pad_to_code_start(self):
        if len(self.code) > self.CODE_START_ADDRESS:
            raise RuntimeError("Code bootstrap exceeds code start address")
        self.code.extend(b"\x00" * (self.CODE_START_ADDRESS - len(self.code)))

    def _current_address(self):
        return len(self.code)

    def _emit_word(self, value):
        self.code.append(value & 0xFF)
        self.code.append((value >> 8) & 0xFF)

    def _emit_i16(self, opcode, immediate, reg=0):
        header = pack_i16_header(opcode, reg1=reg)
        if self.emit_debug:
            print(f"EMIT_I16 addr=0x{self._current_address():04X} op=0x{opcode:02X} reg={reg} imm=0x{immediate & 0xFFFF:04X} func={self.current_function} idx={self.current_instr_index}")
        self._emit_word(header)
        self._emit_word(immediate & 0xFFFF)

    def _emit_r(self, opcode, reg1, reg2=0, size_flag=0, lower_flag=0):
        word = pack_r_type(opcode, reg1, reg2, size_flag=size_flag, lower_flag=lower_flag)
        if self.emit_debug:
            print(f"EMIT_R  addr=0x{self._current_address():04X} op=0x{opcode:02X} r1={reg1} r2={reg2} func={self.current_function} idx={self.current_instr_index}")
        # Removed push/pop balance tracking as it used old frame info dictionary

        self._emit_word(word)

    def _emit_m(self, opcode, reg1, base_reg, offset):
        header = pack_m_type(opcode, reg1, base_reg)
        if self.emit_debug:
            print(f"EMIT_M  addr=0x{self._current_address():04X} op=0x{opcode:02X} r1={reg1} base={base_reg} off=0x{offset & 0xFFFF:04X} func={self.current_function} idx={self.current_instr_index}")
        self._emit_word(header)
        self._emit_word(offset & 0xFFFF)

    def _emit_m_abs(self, opcode, reg1, addr):
        """M-type absolute: reg1 = reg1 OP mem[#addr]  (ALU/CMP). Base register ignored."""
        header = pack_m_type_abs(opcode, reg1)
        if self.emit_debug:
            print(f"EMIT_MABS addr=0x{self._current_address():04X} op=0x{opcode:02X} r1={reg1} mem=0x{addr & 0xFFFF:04X} func={self.current_function} idx={self.current_instr_index}")
        self._emit_word(header)
        self._emit_word(addr & 0xFFFF)

    def _emit_ldroff_abs(self, dst_reg, addr):
        """Absolute load: dst = mem[#addr]  (4 bytes, no scratch register)."""
        header = pack_r_type(OP_LDROFF, dst_reg, 0, size_flag=0, lower_flag=0) | ABS_FLAG
        if self.emit_debug:
            print(f"EMIT_LDABS addr=0x{self._current_address():04X} dst={dst_reg} mem=0x{addr & 0xFFFF:04X} func={self.current_function} idx={self.current_instr_index}")
        self._emit_word(header)
        self._emit_word(addr & 0xFFFF)

    def _mark_label(self, label_name):
        self.labels[label_name] = self._current_address()

    def _emit_branch_fixup(self, opcode, label_name):
        fixup_address = self._current_address()
        self._emit_i16(opcode, 0)
        self.fixups.append((fixup_address + 2, label_name))

    def _resolve_fixups(self):
        for immediate_offset, label_name in self.fixups:
            if label_name not in self.labels:
                raise RuntimeError(f"Undefined label '{label_name}' in backend fixup")
            target = self.labels[label_name] & 0xFFFF
            self.code[immediate_offset] = target & 0xFF
            self.code[immediate_offset + 1] = (target >> 8) & 0xFF

        if len(self.code) > self.INPUT_ADDRESS:
            raise RuntimeError(
                f"ROM image too large ({len(self.code)} bytes). Max allowed is {self.INPUT_ADDRESS} bytes."
            )

    def _new_internal_label(self, prefix):
        name = f"__{prefix}_{self.temp_label_counter}"
        self.temp_label_counter += 1
        return name

    def _emit_ldroff(self, dst_reg, base_reg, offset):
        """Emit LDROFF instruction: dst = memory[base + offset].
        
        Format (4 bytes):
            First word: opcode (0x1C) | dst_reg (3 bits) | base_reg (3 bits) | ...
            Second word: signed offset (16-bit)
        """
        if self.emit_debug:
            print(f"EMIT_LDROFF addr=0x{self._current_address():04X} dst={dst_reg} base={base_reg} off={offset} func={self.current_function} idx={self.current_instr_index}")
        header = pack_r_type(OP_LDROFF, dst_reg, base_reg, size_flag=0, lower_flag=0)
        self._emit_word(header)
        self._emit_word(offset & 0xFFFF)

    def _emit_stroff(self, src_reg, base_reg, offset):
        """Emit STROFF instruction: memory[base + offset] = src.
        
        Format (4 bytes):
            First word: opcode (0x1D) | src_reg (3 bits) | base_reg (3 bits) | ...
            Second word: signed offset (16-bit)
        """
        if self.emit_debug:
            print(f"EMIT_STROFF addr=0x{self._current_address():04X} src={src_reg} base={base_reg} off={offset} func={self.current_function} idx={self.current_instr_index}")
        header = pack_r_type(OP_STROFF, src_reg, base_reg, size_flag=0, lower_flag=0)
        self._emit_word(header)
        self._emit_word(offset & 0xFFFF)

    # ---- Inline assembly (mini-assembler) -------------------------------
    @staticmethod
    def _asm_is_reg(tok):
        t = tok.strip().upper()
        return t.startswith("R") and t[1:].isdigit() and 0 <= int(t[1:]) <= 7

    @staticmethod
    def _asm_reg(tok):
        t = tok.strip().upper()
        if t.startswith("R") and t[1:].isdigit() and 0 <= int(t[1:]) <= 7:
            return int(t[1:])
        raise RuntimeError(f"asm: invalid register '{tok}'")

    @staticmethod
    def _asm_is_number(tok):
        t = tok.strip()
        if t.lower().startswith("0x"):
            try:
                int(t, 16)
                return True
            except ValueError:
                return False
        body = t[1:] if t.startswith("-") else t
        return body.isdigit()

    @staticmethod
    def _asm_imm(tok):
        t = tok.strip()
        if t.lower().startswith("0x"):
            return int(t, 16) & 0xFFFF
        return int(t, 10) & 0xFFFF

    @staticmethod
    def _asm_base(tok):
        return tok.strip().strip("[]")

    def _emit_i16_operand(self, opcode, reg, operand):
        """Emit a 4-byte I-type whose immediate is a number or a label fixup."""
        if self._asm_is_number(operand):
            self._emit_i16(opcode, self._asm_imm(operand), reg=reg)
        else:
            fixup_address = self._current_address()
            self._emit_i16(opcode, 0, reg=reg)
            self.fixups.append((fixup_address + 2, operand))

    def _emit_asm(self, lines):
        # Make labels DEFINED inside this block unique so identical names (e.g. `loop`)
        # in different asm blocks don't collide. References to names NOT defined here
        # (e.g. a global function called via CAL) are left untouched.
        local_labels = set()
        for raw in lines:
            stripped = raw.split(";", 1)[0].split("//", 1)[0].strip()
            if stripped.endswith(":"):
                local_labels.add(stripped[:-1].strip())
        prefix = f"__asm{self.temp_label_counter}_"
        self.temp_label_counter += 1

        def localize(tok):
            return prefix + tok if tok in local_labels else tok

        for raw in lines:
            line = raw.split(";", 1)[0].split("//", 1)[0].strip()
            if not line:
                continue
            if line.endswith(":"):
                self._mark_label(prefix + line[:-1].strip())
                continue
            parts = line.replace(",", " ").split()
            self._emit_asm_line(parts[0].upper(), [localize(o) for o in parts[1:]])

    def _emit_asm_line(self, mnem, ops):
        alu = {
            "ADD": OP_ADD, "SUB": OP_SUB, "AND": OP_AND, "OR": OP_OR, "XOR": OP_XOR,
            "SHL": OP_SHL, "SHR": OP_SHR, "MUL": OP_MUL, "DIV": OP_DIV, "CMP": OP_CMP,
        }
        jmps = {
            "JMP": OP_JMP, "JZ": OP_JZ, "JNZ": OP_JNZ, "JS": OP_JS, "JNS": OP_JNS,
            "JC": OP_JC, "JNC": OP_JNC, "CAL": OP_CAL,
        }

        if mnem == "NOP":
            self._emit_word(pack_r_type(OP_NOP, 0, 0))
        elif mnem == "HLT":
            self._emit_word(pack_r_type(OP_HLT, 0, 0))
        elif mnem == "RET":
            self._emit_word(pack_r_type(OP_RET, 0, 0))
        elif mnem == "LDI":
            self._emit_i16_operand(OP_LDI, self._asm_reg(ops[0]), ops[1])
        elif mnem == "STRI":
            self._emit_i16_operand(OP_STRI, self._asm_reg(ops[0]), ops[1])
        elif mnem == "MOV":
            self._emit_r(OP_MOV, self._asm_reg(ops[0]), self._asm_reg(ops[1]),
                         size_flag=0, lower_flag=0)
        elif mnem in ("PSH", "POP"):
            self._emit_r(OP_PSH if mnem == "PSH" else OP_POP, self._asm_reg(ops[0]), 0)
        elif mnem in ("LDR", "STR"):
            opcode = OP_LDR if mnem == "LDR" else OP_STR
            rd = self._asm_reg(ops[0])
            rb = self._asm_reg(self._asm_base(ops[1]))
            is_byte = any(o.lower() == "byte" for o in ops[2:])
            if is_byte:
                self._emit_r(opcode, rd, rb, size_flag=1, lower_flag=1)
            else:
                self._emit_r(opcode, rd, rb, size_flag=0, lower_flag=0)
        elif mnem in jmps:
            opcode = jmps[mnem]
            if self._asm_is_number(ops[0]):
                self._emit_i16(opcode, self._asm_imm(ops[0]))
            else:
                self._emit_branch_fixup(opcode, ops[0])
        elif mnem in alu:
            opcode = alu[mnem]
            rd = self._asm_reg(ops[0])
            if self._asm_is_reg(ops[1]):
                self._emit_r(opcode, rd, self._asm_reg(ops[1]), size_flag=0, lower_flag=1)
            else:
                # 4-byte I-type (reg OP imm16): size_flag=0, lower_flag=0
                self._emit_i16_operand(opcode, rd, ops[1])
        elif mnem == "DW":
            self._emit_word(self._asm_imm(ops[0]))
        elif mnem == "DB":
            self.code.append(self._asm_imm(ops[0]) & 0xFF)
        else:
            raise RuntimeError(f"asm: unknown mnemonic '{mnem}'")

    def _is_in_function(self):
        """Check if currently inside a function."""
        return self.current_function is not None

    def _emit_prologue(self, function_name):
        frame = self.function_frames[function_name]
        
        # 1. Set FP = SP (FP+0 is return IP)
        self._emit_r(OP_MOV, self.REG_FP, self.REG_SP, size_flag=0, lower_flag=0)
        
        # 2. Push callee-saved registers
        if 4 in frame.callee_saved:
            self._emit_r(OP_PSH, 4)
        if 5 in frame.callee_saved:
            self._emit_r(OP_PSH, 5)
            
        # 3. Allocate local frame (spills)
        if frame.frame_size > 0:
            self._emit_i16(OP_SUB, frame.frame_size, reg=self.REG_SP)

        # 4. Spill address-taken register-params (R1-R3) into their forced stack slots.
        #    Steps 1-3 never touch R1-R3, so the incoming argument values are still intact.
        for reg, fp_offset in frame.location_map.spilled_params:
            self._emit_stroff(reg, self.REG_FP, fp_offset)

    def _emit_epilogue(self, function_name):
        frame = self.function_frames[function_name]
        
        # 1. Deallocate local frame
        if frame.frame_size > 0:
            self._emit_i16(OP_ADD, frame.frame_size, reg=self.REG_SP)
            
        # 2. Pop callee-saved (reverse order)
        if 5 in frame.callee_saved:
            self._emit_r(OP_POP, 5)
        if 4 in frame.callee_saved:
            self._emit_r(OP_POP, 4)
            
        # 3. Restore SP to FP
        self._emit_r(OP_MOV, self.REG_SP, self.REG_FP, size_flag=0, lower_flag=0)
        
        # 4. RET
        self._emit_r(OP_RET, 0, 0, size_flag=0, lower_flag=0)

    def _const_to_word(self, value):
        value = int(value)
        return value & 0xFFFF

    def _ensure_data_address(self, operand, size_bytes=2):
        """Reserve `size_bytes` bytes in the data section for `operand` and return
        the start address.  Idempotent — subsequent calls for the same operand
        return the already-assigned address without re-allocating."""
        if operand not in self.operand_addresses:
            address = self.next_data_address
            if address + size_bytes > self.DATA_END_ADDRESS:
                raise RuntimeError(
                    f"Data section overflow: need {size_bytes} B at 0x{address:04X} "
                    f"but data ceiling is 0x{self.DATA_END_ADDRESS:04X} (code starts there)"
                )
            self.operand_addresses[operand] = address
            self.next_data_address += size_bytes
        return self.operand_addresses[operand]

    def _emit_to_reg(self, operand, reg, frame: FunctionFrame):
        """Load any operand into the given register."""
        if isinstance(operand, Const):
            self._emit_i16(OP_LDI, self._const_to_word(operand.value), reg=reg)
            return

        # If it has a location in the frame, use it
        if frame and (operand in frame.location_map.locations or any(v == operand for v in frame.location_map.locations.keys())):
            loc = frame.loc(operand)
            if loc.kind == "register":
                if loc.register != reg:
                    self._emit_r(OP_MOV, reg, loc.register, size_flag=0, lower_flag=0)
            else: # stack
                self._emit_ldroff(reg, self.REG_FP, loc.fp_offset)
            return

        # Fallback to global data section: absolute load straight into `reg` (4 bytes, no
        # scratch). Previously this used LDI R3,addr; LDR reg,[R3], which clobbered R3 (a live
        # 3rd-argument register).
        addr = self._ensure_data_address(operand)
        self._emit_ldroff_abs(reg, addr)

    def _emit_from_reg(self, operand, reg, frame: FunctionFrame):
        """Store register to operand's location."""
        # If it has a location in the frame, use it
        if frame and (operand in frame.location_map.locations or any(v == operand for v in frame.location_map.locations.keys())):
            loc = frame.loc(operand)
            if loc.kind == "register":
                if loc.register != reg:
                    self._emit_r(OP_MOV, loc.register, reg, size_flag=0, lower_flag=0)
            else: # stack
                self._emit_stroff(reg, self.REG_FP, loc.fp_offset)
            return

        # Fallback to global data section
        addr = self._ensure_data_address(operand)
        self._emit_i16(OP_STRI, addr, reg=reg)

    # ---- Operand-form helpers (operand-aware codegen) -------------------------
    def _home_reg(self, operand, frame):
        """Register number if `operand` lives in a register, else None.
        Const, stack-spilled, and global operands all return None."""
        if isinstance(operand, Const):
            return None
        if frame and (operand in frame.location_map.locations
                      or any(v == operand for v in frame.location_map.locations)):
            loc = frame.loc(operand)
            return loc.register if loc.kind == "register" else None
        return None

    def _same_home(self, a, b, frame):
        """True iff `a` and `b` are both spilled to the same stack slot, so a copy between
        them is a no-op (the allocator coalesced two non-interfering vars to one slot)."""
        if not frame:
            return False
        a_in = (a in frame.location_map.locations or any(v == a for v in frame.location_map.locations))
        b_in = (b in frame.location_map.locations or any(v == b for v in frame.location_map.locations))
        if a_in and b_in:
            la, lb = frame.loc(a), frame.loc(b)
            return la.kind == "stack" and lb.kind == "stack" and la.fp_offset == lb.fp_offset
        return False

    def _operand_form(self, operand, frame):
        """How to address `operand` in place as the RHS of an ALU/CMP op:
              ("imm", value)     -> I16          (4 B)
              ("reg", r)         -> R-type       (2 B)
              ("stk", fp_offset) -> M-type FP    (4 B)
              ("abs", addr)      -> M-type abs   (4 B)  [global scalar]
        Never loads the operand into a scratch register."""
        if isinstance(operand, Const):
            return ("imm", self._const_to_word(operand.value))
        if frame and (operand in frame.location_map.locations
                      or any(v == operand for v in frame.location_map.locations)):
            loc = frame.loc(operand)
            if loc.kind == "register":
                return ("reg", loc.register)
            return ("stk", loc.fp_offset)
        return ("abs", self._ensure_data_address(operand))

    def _emit_op_rhs(self, opcode, acc_reg, form):
        """Emit `acc_reg = acc_reg <opcode> form` for ALU or CMP, choosing the in-place
        encoding from `form`. The second operand is never materialized into a register."""
        kind = form[0]
        if kind == "imm":
            self._emit_i16(opcode, form[1], reg=acc_reg)
        elif kind == "reg":
            self._emit_r(opcode, acc_reg, form[1], size_flag=0, lower_flag=1)
        elif kind == "stk":
            self._emit_m(opcode, acc_reg, self.REG_FP, form[1])
        elif kind == "abs":
            self._emit_m_abs(opcode, acc_reg, form[1])

    def _emit_instruction(self, instruction):
        op = instruction.op

        # ---- Dead-store elimination ---------------------------------------------------------
        # This instruction's result is never read again, so emitting it is pointless. Skipping it
        # ALSO fixes the dead-store aliasing bug: the allocator may give two variables the same
        # register when their live ranges don't overlap, but a write to a variable AFTER its last
        # use (a dead store) would still hit that shared register and corrupt the other, still-live
        # variable. Not emitting dead stores makes that corruption impossible.
        #
        # SAFE ONLY because the pre-pass (_DSE_OPS) marks pure, side-effect-free defs (arithmetic /
        # copies / comparisons / addrof). It NEVER marks call, store_ptr, load_ptr, STRI, or def_arr
        # — those have effects (or may touch MMIO) and must run even when their result is unused.
        if id(instruction) in self.dead_instr:
            self.current_instr_index += 1
            return
        # -------------------------------------------------------------------------------------

        if op == "entry_begin":
            # Entry block: globals are initialized here, then main() is called.
            # We mark the entry label so bootstrap jump lands here.
            return
        
        if op == "entry_end":
            # After entry block, halt the CPU
            self._emit_i16(OP_HLT, 0)
            return

        if op == "func_begin":
            self._mark_label(instruction.arg1)
            self.current_function = instruction.arg1
            self.current_instr_index = 0
            frame = self.function_frames.get(instruction.arg1)
            if not (frame and frame.is_naked):
                self._emit_prologue(instruction.arg1)
            return

        if op == "param":
            return

        if op == "asm":
            self._emit_asm(instruction.arg1)
            self.current_instr_index += 1
            return

        if op == "func_end":
            if self.current_function:
                frame = self.function_frames.get(self.current_function)
                if frame and frame.is_naked:
                    pass  # asm body supplies its own RET
                else:
                    self._mark_label(frame.epilogue_label)
                    self._emit_epilogue(self.current_function)
            self.current_function = None
            return

        if op == "label":
            self._mark_label(instruction.result)
            return

        if op == "goto":
            self._emit_branch_fixup(OP_JMP, instruction.result)
            self.current_instr_index += 1
            return

        if op == "if":
            if id(instruction) in self.skip_if:
                # The preceding comparison was fused and already emitted the branch to our target.
                self.current_instr_index += 1
                return
            frame = self.function_frames.get(self.current_function)
            # Compare the condition in place when it already lives in a register (skip the MOV).
            creg = self._home_reg(instruction.arg1, frame)
            if creg is None:
                self._emit_to_reg(instruction.arg1, self.REG_RETVAL, frame)
                creg = self.REG_RETVAL
            self._emit_i16(OP_CMP, 0, reg=creg)
            self._emit_branch_fixup(OP_JNZ, instruction.result)
            self.current_instr_index += 1
            return

        if op == "addr_label":
            # base = &label  (LDI base, <label-addr>; resolved via the fixup table in pass 2).
            frame = self.function_frames.get(self.current_function)
            res_reg = self._home_reg(instruction.result, frame)
            dst = res_reg if res_reg is not None else self.REG_RETVAL
            self._emit_i16_operand(OP_LDI, dst, instruction.arg1)   # arg1 = table label name
            if res_reg is None:
                self._emit_from_reg(instruction.result, self.REG_RETVAL, frame)
            self.current_instr_index += 1
            return

        if op == "goto_reg":
            # Indirect jump: pc = target register (JMP register-mode, lower_flag=1).
            frame = self.function_frames.get(self.current_function)
            reg = self._home_reg(instruction.arg1, frame)
            if reg is None:
                self._emit_to_reg(instruction.arg1, self.REG_RETVAL, frame)
                reg = self.REG_RETVAL
            self._emit_r(OP_JMP, reg, size_flag=0, lower_flag=1)
            self.current_instr_index += 1
            return

        if op == "jump_table":
            # Emit the address table inline (one word per index; never executed — the indirect jump
            # skips over it). Each entry is a fixup to a case/default label, patched in pass 2.
            self._mark_label(instruction.arg1)
            for label_name in instruction.extra:
                fixup_address = self._current_address()
                self._emit_word(0)
                self.fixups.append((fixup_address, label_name))
            self.current_instr_index += 1
            return

        if op in ("def", "eq"):
            if instruction.arg1 is None:
                self.current_instr_index += 1
                return

            frame = self.function_frames.get(self.current_function)
            src, dst = instruction.arg1, instruction.result
            dst_reg = self._home_reg(dst, frame)          # None => dst on stack/global
            if isinstance(src, Const):
                if dst_reg is not None:
                    self._emit_i16(OP_LDI, self._const_to_word(src.value), reg=dst_reg)
                else:
                    self._emit_to_reg(src, self.REG_RETVAL, frame)
                    self._emit_from_reg(dst, self.REG_RETVAL, frame)
            else:
                src_reg = self._home_reg(src, frame)
                if dst_reg is not None and src_reg is not None:
                    if dst_reg != src_reg:
                        self._emit_r(OP_MOV, dst_reg, src_reg, size_flag=0, lower_flag=0)
                    # same register -> emit nothing
                elif dst_reg is not None:
                    self._emit_to_reg(src, dst_reg, frame)        # stack/global -> register (1 instr)
                elif src_reg is not None:
                    self._emit_from_reg(dst, src_reg, frame)      # register -> stack/global (1 instr)
                elif self._same_home(src, dst, frame):
                    pass                                          # coalesced to same slot -> no-op
                else:
                    # stack/global -> stack/global: bounce through R0.
                    self._emit_to_reg(src, self.REG_RETVAL, frame)
                    self._emit_from_reg(dst, self.REG_RETVAL, frame)
            self.current_instr_index += 1
            return

        if op in self.ALU_OPCODE:
            # result = arg1 OP arg2.  arg2 is addressed in place via _operand_form/_emit_op_rhs
            # (immediate / register / stack / global) — never loaded into a scratch register.
            # Only arg1 is moved into an accumulator, because the ALU's first operand must be a
            # register and is overwritten by the result.
            frame   = self.function_frames.get(self.current_function)
            alu     = self.ALU_OPCODE[op]
            res_reg = self._home_reg(instruction.result, frame)   # None => result on stack/global
            a2_reg  = self._home_reg(instruction.arg2, frame)
            form2   = self._operand_form(instruction.arg2, frame)

            if res_reg is not None:
                if a2_reg is not None and a2_reg == res_reg:
                    # arg2 already occupies the destination register.
                    if op in self.COMMUTATIVE:
                        # res = arg2 OP arg1  ==  arg1 OP arg2
                        self._emit_op_rhs(alu, res_reg, self._operand_form(instruction.arg1, frame))
                    else:
                        # e.g. x = y - x: compute in R0 so arg2 is read from res_reg before overwrite.
                        self._emit_to_reg(instruction.arg1, self.REG_RETVAL, frame)
                        self._emit_op_rhs(alu, self.REG_RETVAL, form2)
                        self._emit_r(OP_MOV, res_reg, self.REG_RETVAL, size_flag=0, lower_flag=0)
                else:
                    a1_reg = self._home_reg(instruction.arg1, frame)
                    if a1_reg != res_reg:
                        # Load arg1 into the destination. Safe: arg2 is not in res_reg here, and a
                        # global arg1 loads via LDROFF-abs (no scratch), so nothing live is clobbered.
                        self._emit_to_reg(instruction.arg1, res_reg, frame)
                    # else arg1 already in dest -> operate in place (e.g. i = i + 1)
                    self._emit_op_rhs(alu, res_reg, form2)
            else:
                # Result lives on the stack/global: compute in R0, then store out.
                self._emit_to_reg(instruction.arg1, self.REG_RETVAL, frame)
                self._emit_op_rhs(alu, self.REG_RETVAL, form2)
                self._emit_from_reg(instruction.result, self.REG_RETVAL, frame)
            self.current_instr_index += 1
            return

        if op == "arg":
            # Collect argument in pending buffer
            # Will be processed when call instruction is encountered
            self.pending_args.append(instruction.arg1)
            return

        if op in ("<", "<=", ">", ">=", "==", "!="):
            self._emit_comparison(instruction)
            return

        if op == "call":
            frame = self.function_frames.get(self.current_function)
            
            # Use the TAC-specified arg count
            arg_count = int(instruction.arg2)
            call_args = self.pending_args[-arg_count:] if arg_count > 0 else []
            self.pending_args = self.pending_args[:-arg_count] if arg_count > 0 else self.pending_args
            stack_arg_count = max(0, arg_count - 3)
            
            arg_registers = [self.REG_ARG1, self.REG_ARG2, self.REG_ARG3]

            # --- Step 1: Spill only the caller-saved regs that hold a value live across the call
            # (precomputed from liveness in _precompute_marks). The callee may clobber any of
            # R1-R3, so we save each one holding a live-across variable. ---
            caller_saved_to_spill = self.call_spill.get(id(instruction), [])
            for reg in caller_saved_to_spill:
                self._emit_r(OP_PSH, reg)

            # --- Step 2: Push stack arguments right-to-left ---
            for i in range(arg_count - 1, 2, -1):
                self._emit_to_reg(call_args[i], self.REG_RETVAL, frame)
                self._emit_r(OP_PSH, self.REG_RETVAL)

            # --- Step 3: Load register arguments (option (i): direct loads) ---
            # The only clobber hazard is an arg whose VALUE currently lives in one of R1-R3
            # (loading args in place could overwrite a not-yet-read source — e.g. f(b, a)).
            # Constants / stack / globals / R4-R5 sources never conflict, so load them directly.
            # Only when some arg is R1-R3-sourced do we fall back to the safe push-all-then-pop.
            num_reg_args = min(3, arg_count)
            conflict = any(self._home_reg(call_args[i], frame) in arg_registers
                           for i in range(num_reg_args))
            if not conflict:
                for i in range(num_reg_args):
                    self._emit_to_reg(call_args[i], arg_registers[i], frame)
            else:
                for i in range(num_reg_args):
                    self._emit_to_reg(call_args[i], self.REG_RETVAL, frame)
                    self._emit_r(OP_PSH, self.REG_RETVAL)
                for i in range(num_reg_args - 1, -1, -1):
                    self._emit_r(OP_POP, arg_registers[i])
            
            # --- Step 4: Push FP (caller saves frame pointer) ---
            self._emit_r(OP_PSH, self.REG_FP)
            
            # --- Step 5: CALL ---
            self._emit_branch_fixup(OP_CAL, instruction.arg1)
            
            # --- Step 6: Pop FP (caller restores frame pointer) ---
            self._emit_r(OP_POP, self.REG_FP)
            
            # --- Step 7: Clean up stack arguments ---
            if stack_arg_count > 0:
                cleanup_size = stack_arg_count * 2
                self._emit_i16(OP_ADD, cleanup_size, reg=self.REG_SP)
            
            # --- Step 8: Restore spilled caller-saved regs (reverse order) ---
            for reg in reversed(caller_saved_to_spill):
                self._emit_r(OP_POP, reg)
            
            # --- Step 9: Store return value if expected ---
            if instruction.result is not None:
                self._emit_from_reg(instruction.result, self.REG_RETVAL, frame)
            
            self.current_instr_index += 1
            return

        # ── M2: Arrays ────────────────────────────────────────────────────────
        if op == "def_arr":
            # Reserve data-section space for an array.  No runtime code emitted;
            # the ROM is zero-initialised in the data region.
            arr_var = instruction.result
            elem_type = instruction.arg1   # "int" or "byte"
            count = instruction.arg2       # element count (int)
            elem_size = 2 if elem_type == "int" else 1
            total = count * elem_size
            addr = self._ensure_data_address(arr_var, total)
            # Array literal: bake the constant elements into the ROM image (back-patched in
            # generate(), after addresses are final). byte -> 1 B; int -> 2 B little-endian.
            if instruction.extra:
                byts = bytearray()
                for v in instruction.extra:
                    if elem_size == 1:
                        byts.append(v & 0xFF)
                    else:
                        byts.append(v & 0xFF)
                        byts.append((v >> 8) & 0xFF)
                self.data_initializers.append((addr, bytes(byts)))
            self.current_instr_index += 1
            return

        if op == "addrof":
            # Compute the runtime address of a variable into REG_RETVAL.
            frame = self.function_frames.get(self.current_function)
            var = instruction.arg1

            in_frame = (frame and
                        (var in frame.location_map.locations or
                         any(v == var for v in frame.location_map.locations)))
            if in_frame:
                loc = frame.loc(var)
                if loc.kind == "stack":
                    # address = FP + fp_offset  (fp_offset is negative for locals)
                    self._emit_r(OP_MOV, self.REG_RETVAL, self.REG_FP,
                                 size_flag=0, lower_flag=0)
                    offset = loc.fp_offset
                    if offset > 0:
                        self._emit_i16(OP_ADD, offset & 0xFFFF,
                                       reg=self.REG_RETVAL)
                    elif offset < 0:
                        self._emit_i16(OP_SUB, (-offset) & 0xFFFF,
                                       reg=self.REG_RETVAL)
                    # offset == 0 would be the return-address slot — shouldn't happen
                else:
                    raise RuntimeError(
                        f"addrof: variable '{getattr(var,'name',var)}' is in a "
                        f"register but its address was taken — allocator should have "
                        f"forced it to a stack slot."
                    )
            else:
                # Global scalar or array: address is in the data section.
                if var in self.operand_addresses:
                    addr = self.operand_addresses[var]
                else:
                    addr = self._ensure_data_address(var, 2)
                self._emit_i16(OP_LDI, addr, reg=self.REG_RETVAL)

            self._emit_from_reg(instruction.result, self.REG_RETVAL, frame)
            self.current_instr_index += 1
            return

        if op == "load_ptr":
            # result = mem[arg1].  Load the pointer value into the destination register itself and
            # dereference in place (self-base, safe after the LDR rd==rb hardening). Bytes are
            # zero-extended with a post-load mask. No scratch register, no R3 clobber.
            frame = self.function_frames.get(self.current_function)
            width = getattr(instruction.result, "type", "int")
            res_reg = self._home_reg(instruction.result, frame)
            dst = res_reg if res_reg is not None else self.REG_RETVAL
            self._emit_to_reg(instruction.arg1, dst, frame)             # dst = pointer value
            if width == "byte":
                self._emit_r(OP_LDR, dst, dst, size_flag=1, lower_flag=1)   # dst.low = mem[dst]
                self._emit_i16(OP_AND, 0x00FF, reg=dst)                     # zero-extend
            else:
                self._emit_r(OP_LDR, dst, dst, size_flag=0, lower_flag=0)   # dst = mem[dst]
            if res_reg is None:
                self._emit_from_reg(instruction.result, self.REG_RETVAL, frame)
            self.current_instr_index += 1
            return

        if op == "store_ptr":
            # mem[arg2] = arg1.  Use the pointer's register directly as the base when it has one.
            frame = self.function_frames.get(self.current_function)
            # Prefer the explicit destination element type (set for arr[i]=val by the TAC
            # generator) over the value's type. Array element stores must match the array's
            # element width; a byte array element must be a 1-byte store even when the value
            # is an int-typed constant/expression. Falls back to the value type for
            # untyped pointer stores (*ptr = val), which intentionally default to word width.
            width = instruction.extra if instruction.extra else getattr(instruction.arg1, "type", "int")
            sf, lf = (1, 1) if width == "byte" else (0, 0)
            p_reg = self._home_reg(instruction.arg2, frame)   # pointer
            v_reg = self._home_reg(instruction.arg1, frame)   # value
            if p_reg is not None:
                if v_reg is not None:
                    self._emit_r(OP_STR, v_reg, p_reg, size_flag=sf, lower_flag=lf)
                else:
                    self._emit_to_reg(instruction.arg1, self.REG_RETVAL, frame)
                    self._emit_r(OP_STR, self.REG_RETVAL, p_reg, size_flag=sf, lower_flag=lf)
            else:
                self._emit_to_reg(instruction.arg2, self.REG_RETVAL, frame)   # R0 = address
                if v_reg is not None:
                    self._emit_r(OP_STR, v_reg, self.REG_RETVAL, size_flag=sf, lower_flag=lf)
                else:
                    # Both pointer and value lack a register: guard R4 to hold the value.
                    # Rare (e.g. *p = <const/stack/global> with p spilled).
                    self._emit_r(OP_PSH, self.REG_SAVED1)
                    self._emit_to_reg(instruction.arg1, self.REG_SAVED1, frame)
                    self._emit_r(OP_STR, self.REG_SAVED1, self.REG_RETVAL, size_flag=sf, lower_flag=lf)
                    self._emit_r(OP_POP, self.REG_SAVED1)
            self.current_instr_index += 1
            return

        if op == "ret":
            frame = self.function_frames.get(self.current_function)
            if instruction.arg1 is not None:
                self._emit_to_reg(instruction.arg1, self.REG_RETVAL, frame)
                
            if frame:
                self._emit_branch_fixup(OP_JMP, frame.epilogue_label)
            else:
                self._emit_r(OP_RET, 0, 0, size_flag=0, lower_flag=0)
            self.current_instr_index += 1
            return

        if op == "print":
            raise RuntimeError("Print is not supported in EmuBackend")

        raise RuntimeError(f"Unsupported TAC op for EmuBackend: {op}")

    def _emit_op_into_flags(self, instruction, frame):
        """Emit the CMP for a comparison: arg1 in place (CMP doesn't write register1) or loaded
        into R0, then arg2 addressed in place via _operand_form. Leaves the result in the flags."""
        a1_reg = self._home_reg(instruction.arg1, frame)
        if a1_reg is None:
            self._emit_to_reg(instruction.arg1, self.REG_RETVAL, frame)
            cmp_reg = self.REG_RETVAL
        else:
            cmp_reg = a1_reg
        self._emit_op_rhs(OP_CMP, cmp_reg, self._operand_form(instruction.arg2, frame))

    def _emit_fused_branch(self, op, target):
        """Branch to `target` when comparison `op` is TRUE, using flags from a preceding CMP.
        Mirrors the per-op jump logic of the 0/1 materialization, but jumps to the real target."""
        if op == "<":
            self._emit_branch_fixup(OP_JS, target)
        elif op == ">=":
            self._emit_branch_fixup(OP_JNS, target)
        elif op == "==":
            self._emit_branch_fixup(OP_JZ, target)
        elif op == "!=":
            self._emit_branch_fixup(OP_JNZ, target)
        elif op == "<=":                                  # sign OR zero
            self._emit_branch_fixup(OP_JS, target)
            self._emit_branch_fixup(OP_JZ, target)
        elif op == ">":                                   # NOT sign AND NOT zero
            skip = self._new_internal_label("fcmp_skip")
            self._emit_branch_fixup(OP_JS, skip)
            self._emit_branch_fixup(OP_JZ, skip)
            self._emit_branch_fixup(OP_JMP, target)
            self._mark_label(skip)
        else:
            raise RuntimeError(f"Unsupported comparison op: {op}")

    def _emit_comparison(self, instruction):
        frame = self.function_frames.get(self.current_function)

        # FUSED: result is consumed only by the immediately-following `if` (that `if` is in
        # self.skip_if). Emit the CMP and branch straight to the if's target — no 0/1 materialization.
        target = self.fused_cmp.get(id(instruction))
        if target is not None:
            self._emit_op_into_flags(instruction, frame)
            self._emit_fused_branch(instruction.op, target)
            self.current_instr_index += 1
            return

        # Unfused: materialize a 0/1 result.
        # CMP does not write register1, so if arg1 already lives in a register we compare it in
        # place (no MOV, no clobber). arg2 is addressed in place via _operand_form — never loaded
        # into a scratch register.
        self._emit_op_into_flags(instruction, frame)

        true_label = self._new_internal_label("cmp_true")
        end_label = self._new_internal_label("cmp_end")

        if instruction.op == "==":
            self._emit_branch_fixup(OP_JZ, true_label)
        elif instruction.op == "!=":
            self._emit_branch_fixup(OP_JNZ, true_label)
        elif instruction.op == "<":
            self._emit_branch_fixup(OP_JS, true_label)
        elif instruction.op == ">=":
            self._emit_branch_fixup(OP_JNS, true_label)
        elif instruction.op == ">":
            # lhs > rhs <=> not(sign) and not(zero)
            fail_label = self._new_internal_label("cmp_fail")
            self._emit_branch_fixup(OP_JS, fail_label)
            self._emit_branch_fixup(OP_JZ, fail_label)
            self._emit_branch_fixup(OP_JMP, true_label)
            self._mark_label(fail_label)
        elif instruction.op == "<=":
            # lhs <= rhs <=> sign or zero
            self._emit_branch_fixup(OP_JS, true_label)
            self._emit_branch_fixup(OP_JZ, true_label)
        else:
            raise RuntimeError(f"Unsupported comparison op: {instruction.op}")

        self._emit_i16(OP_LDI, 0, reg=self.REG_RETVAL)
        self._emit_branch_fixup(OP_JMP, end_label)
        self._mark_label(true_label)
        self._emit_i16(OP_LDI, 1, reg=self.REG_RETVAL)
        self._mark_label(end_label)

        self._emit_from_reg(instruction.result, self.REG_RETVAL, frame)
        self.current_instr_index += 1

