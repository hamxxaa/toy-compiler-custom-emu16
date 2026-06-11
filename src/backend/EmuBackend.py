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
)
from .core.liveness import analyze_liveness
from .core.allocator import allocate_function, LocationMap
from .core.function_frame import FunctionFrame


class EmuBackend:
    # Memory layout:
    #   0x0000-0x0007  Bootstrap (LDI sp + JMP code_start)
    #   0x0008-0x0FFF  Data section (~4 KB for globals + arrays, grows up from 0x0008)
    #   0x1000-0xADFD  Code section (functions, grows up from 0x1000)
    #   0xADFE         Stack (grows DOWN from here toward code)
    #   0xADFF         INPUT (hardware mapped)
    #   0xAE00-0xAFFF  PRAM (hardware mapped, 512 bytes)
    #   0xB000-0xFFFF  VRAM (hardware mapped, 20480 bytes)
    CODE_START_ADDRESS = 0x1000
    DATA_START_ADDRESS = 0x0008
    STACK_START_ADDRESS = 0xADFE
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

    def __init__(self, tac, address_taken=None):
        self.tac = tac
        self.address_taken = address_taken or set()
        self.code = bytearray()
        self.labels = {}
        self.fixups = []
        self.temp_label_counter = 0
        self.operand_addresses = {}
        self.next_data_address = self.DATA_START_ADDRESS
        
        # Function context for prologue/epilogue and allocation
        self.current_function = None
        # Maps function name -> FunctionFrame
        self.function_frames = {}
        
        # Argument passing buffer: collects arguments until call instruction
        self.pending_args = []  # List of operands to be passed as arguments
        
        # Current instruction index within a function
        self.current_instr_index = 0
        # Enable emission debug if environment variable EMU_EMIT_DEBUG=1
        self.emit_debug = True


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
                                        self.address_taken)
            
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
        return bytes(self.code)

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

    def _has_more_instructions_in_function(self, current_instr):
        """Check if there are more instructions after current_instr in the current function.
        
        Returns True if there are instructions before func_end.
        """
        if not self.current_function:
            return False
        
        found_current = False
        for instr in self.tac.instructions:
            if instr is current_instr:
                found_current = True
                continue
            
            if found_current:
                # After finding current instruction
                if instr.op == "func_end" and instr.arg1 == self.current_function:
                    # Reached end of function
                    return False
                elif instr.op == "func_end":
                    # Different function end
                    continue
                else:
                    # Found another instruction in same function
                    return True
        
        return False

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
            if address + size_bytes > self.CODE_START_ADDRESS:
                raise RuntimeError(
                    f"Data section overflow: need {size_bytes} B at 0x{address:04X} "
                    f"but code starts at 0x{self.CODE_START_ADDRESS:04X}"
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

        # Fallback to global data section
        addr = self._ensure_data_address(operand)
        self._emit_i16(OP_LDI, addr, reg=self.REG_ADDR)
        self._emit_r(OP_LDR, reg, self.REG_ADDR, size_flag=0, lower_flag=0)

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

    def _emit_instruction(self, instruction):
        op = instruction.op

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
            frame = self.function_frames.get(self.current_function)
            self._emit_to_reg(instruction.arg1, self.REG_RETVAL, frame)
            self._emit_i16(OP_CMP, 0, reg=self.REG_RETVAL)
            self._emit_branch_fixup(OP_JNZ, instruction.result)
            self.current_instr_index += 1
            return

        if op in ("def", "eq"):
            if instruction.arg1 is None:
                self.current_instr_index += 1
                return
            
            frame = self.function_frames.get(self.current_function)
            self._emit_to_reg(instruction.arg1, self.REG_RETVAL, frame)
            self._emit_from_reg(instruction.result, self.REG_RETVAL, frame)
            self.current_instr_index += 1
            return

        if op in ("+", "-", "&", "|", "*", "/", "shl", "shr"):
            frame = self.function_frames.get(self.current_function)
            self._emit_to_reg(instruction.arg1, self.REG_RETVAL, frame)

            # Check arg2 location
            arg2_loc = None
            if frame and (instruction.arg2 in frame.location_map.locations or any(v == instruction.arg2 for v in frame.location_map.locations.keys())):
                arg2_loc = frame.loc(instruction.arg2)

            opcode_map = {
                "+": OP_ADD, "-": OP_SUB, "&": OP_AND, "|": OP_OR,
                "*": OP_MUL, "/": OP_DIV, "shl": OP_SHL, "shr": OP_SHR,
            }
            alu_op = opcode_map[op]
            
            if arg2_loc and arg2_loc.kind == "register":
                self._emit_r(alu_op, self.REG_RETVAL, arg2_loc.register, size_flag=0, lower_flag=1)
            elif arg2_loc and arg2_loc.kind == "stack":
                # Use M-type!
                self._emit_m(alu_op, self.REG_RETVAL, self.REG_FP, arg2_loc.fp_offset)
            else:
                # const or global
                self._emit_to_reg(instruction.arg2, self.REG_SAVED1, frame)
                self._emit_r(alu_op, self.REG_RETVAL, self.REG_SAVED1, size_flag=0, lower_flag=1)
            
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

        if op == "arg":
            # Collect argument in pending buffer
            # Will be processed when call instruction is encountered
            self.pending_args.append(instruction.arg1)
            self.current_instr_index += 1
            return

        if op == "call":
            frame = self.function_frames.get(self.current_function)
            
            # Use the TAC-specified arg count
            arg_count = int(instruction.arg2)
            call_args = self.pending_args[-arg_count:] if arg_count > 0 else []
            self.pending_args = self.pending_args[:-arg_count] if arg_count > 0 else self.pending_args
            stack_arg_count = max(0, arg_count - 3)
            
            # --- Step 1: Determine which caller-saved regs to spill ---
            has_more = self._has_more_instructions_in_function(instruction)
            caller_saved_to_spill = []
            if has_more and self.current_function:
                caller_saved_to_spill = [self.REG_ARG1, self.REG_ARG2, self.REG_ARG3]
            
            for reg in caller_saved_to_spill:
                self._emit_r(OP_PSH, reg)
            
            # --- Step 2: Push stack arguments right-to-left ---
            for i in range(arg_count - 1, 2, -1):
                self._emit_to_reg(call_args[i], self.REG_RETVAL, frame)
                self._emit_r(OP_PSH, self.REG_RETVAL)
            
            # --- Step 3: Load register arguments safely ---
            # To avoid clobbering, push all register args, then pop them into place
            num_reg_args = min(3, arg_count)
            for i in range(num_reg_args):
                self._emit_to_reg(call_args[i], self.REG_RETVAL, frame)
                self._emit_r(OP_PSH, self.REG_RETVAL)
                
            arg_registers = [self.REG_ARG1, self.REG_ARG2, self.REG_ARG3]
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
            self._ensure_data_address(arr_var, total)
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
            # Load a word or byte from the address stored in arg1.
            # result.type tells us the access width.
            frame = self.function_frames.get(self.current_function)
            self._emit_to_reg(instruction.arg1, self.REG_ADDR, frame)
            result_type = getattr(instruction.result, "type", "int")
            if result_type == "byte":
                # Zero-extend: LDI R0, 0 then LDR R0, [R3], byte
                self._emit_i16(OP_LDI, 0, reg=self.REG_RETVAL)
                self._emit_r(OP_LDR, self.REG_RETVAL, self.REG_ADDR,
                             size_flag=1, lower_flag=1)
            else:
                self._emit_r(OP_LDR, self.REG_RETVAL, self.REG_ADDR,
                             size_flag=0, lower_flag=0)
            self._emit_from_reg(instruction.result, self.REG_RETVAL, frame)
            self.current_instr_index += 1
            return

        if op == "store_ptr":
            # Store arg1 (value) to the address in arg2 (pointer).
            # arg1.type tells us the access width.
            frame = self.function_frames.get(self.current_function)
            self._emit_to_reg(instruction.arg1, self.REG_RETVAL, frame)
            self._emit_to_reg(instruction.arg2, self.REG_ADDR, frame)
            val_type = getattr(instruction.arg1, "type", "int")
            if val_type == "byte":
                self._emit_r(OP_STR, self.REG_RETVAL, self.REG_ADDR,
                             size_flag=1, lower_flag=1)
            else:
                self._emit_r(OP_STR, self.REG_RETVAL, self.REG_ADDR,
                             size_flag=0, lower_flag=0)
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

    def _emit_comparison(self, instruction):
        frame = self.function_frames.get(self.current_function)
        self._emit_to_reg(instruction.arg1, self.REG_RETVAL, frame)
        
        # Check arg2 location
        arg2_loc = None
        if frame and (instruction.arg2 in frame.location_map.locations or any(v == instruction.arg2 for v in frame.location_map.locations.keys())):
            arg2_loc = frame.loc(instruction.arg2)
            
        if arg2_loc and arg2_loc.kind == "register":
            self._emit_r(OP_CMP, self.REG_RETVAL, arg2_loc.register, size_flag=0, lower_flag=1)
        elif arg2_loc and arg2_loc.kind == "stack":
            self._emit_m(OP_CMP, self.REG_RETVAL, self.REG_FP, arg2_loc.fp_offset)
        else:
            # const or global
            self._emit_to_reg(instruction.arg2, self.REG_SAVED1, frame)
            self._emit_r(OP_CMP, self.REG_RETVAL, self.REG_SAVED1, size_flag=0, lower_flag=1)

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

