from dataclasses import dataclass, field
from typing import List, Set, Dict, Tuple, Optional
from codegen.TACGenerator import TempVar, Var, Const

@dataclass
class LivenessInfo:
    live_at: List[Set[any]]              # len = len(instructions) + 1
    intervals: Dict[any, Tuple[int, int]]
    max_simultaneous_live: int

@dataclass
class BasicBlock:
    start_idx: int
    end_idx: int
    instructions: List[any]
    successors: List['BasicBlock'] = field(default_factory=list)
    predecessors: List['BasicBlock'] = field(default_factory=list)
    live_in: Set[any] = field(default_factory=set)
    live_out: Set[any] = field(default_factory=set)

CONTROL_OPS = {"goto", "label", "if", "arg", "ret", "func_begin", "func_end", "entry_begin", "entry_end", "param", "goto_reg", "jump_table"}

def _is_value_operand(operand):
    return isinstance(operand, (Var, TempVar))

def _defines_result(instruction):
    return _is_value_operand(instruction.result) and instruction.op not in CONTROL_OPS

def build_cfg(instructions: List[any]) -> List[BasicBlock]:
    if not instructions:
        return []

    # 1. Identify block boundaries
    block_starts = {0}
    for i, instr in enumerate(instructions):
        if instr.op == "label":
            block_starts.add(i)
        elif instr.op in ("goto", "if", "ret", "goto_reg"):
            if i + 1 < len(instructions):
                block_starts.add(i + 1)
    
    sorted_starts = sorted(list(block_starts))
    blocks = []
    
    label_to_block = {}
    
    # 2. Create blocks
    for idx, start in enumerate(sorted_starts):
        end = sorted_starts[idx + 1] if idx + 1 < len(sorted_starts) else len(instructions)
        block = BasicBlock(start_idx=start, end_idx=end, instructions=instructions[start:end])
        blocks.append(block)
        
        # If block starts with a label, record it
        if block.instructions and block.instructions[0].op == "label":
            label_name = block.instructions[0].result
            label_to_block[label_name] = block

    # 3. Connect edges
    for i, block in enumerate(blocks):
        last_instr = block.instructions[-1]
        
        if last_instr.op == "goto":
            target = label_to_block.get(last_instr.result)
            if target:
                block.successors.append(target)
                target.predecessors.append(block)
                
        elif last_instr.op == "if":
            # taken edge
            target = label_to_block.get(last_instr.result)
            if target:
                block.successors.append(target)
                target.predecessors.append(block)
            # fallthrough edge
            if i + 1 < len(blocks):
                block.successors.append(blocks[i + 1])
                blocks[i + 1].predecessors.append(block)
                
        elif last_instr.op == "goto_reg":
            # Indirect jump: connect to every case/default block named in `extra`. No fallthrough.
            for label_name in (last_instr.extra or []):
                target = label_to_block.get(label_name)
                if target and target not in block.successors:
                    block.successors.append(target)
                    target.predecessors.append(block)

        elif last_instr.op == "ret":
            # no successors
            pass

        else:
            # fallthrough
            if i + 1 < len(blocks):
                block.successors.append(blocks[i + 1])
                blocks[i + 1].predecessors.append(block)
                
    return blocks

def _extend_arg_liveness(instructions: List[any], live_at: List[Set[any]]) -> None:
    pending_calls = []  # stack of (call_instr_index, remaining_args)
    
    for i in range(len(instructions) - 1, -1, -1):
        instr = instructions[i]
        
        if instr.op == "call":
            arg_count = int(instr.arg2) if instr.arg2 is not None else 0
            if arg_count > 0:
                pending_calls.append([i, arg_count])
                
        elif instr.op == "arg":
            if pending_calls:
                call_idx, remaining = pending_calls[-1]
                operand = instr.arg1
                
                # Make the operand live from this `arg` instruction all the way down to the `call`
                if _is_value_operand(operand):
                    for j in range(i, call_idx + 1):
                        live_at[j].add(operand)
                
                remaining -= 1
                if remaining == 0:
                    pending_calls.pop()
                else:
                    pending_calls[-1][1] = remaining

def analyze_liveness(instructions: List[any]) -> LivenessInfo:
    blocks = build_cfg(instructions)
    line_count = len(instructions)
    
    # Identify params to treat them as defined at index 0
    params = set()
    for instr in instructions:
        if instr.op == "param":
            pass # Currently param operands are just strings "name:type" in TAC
            # Wait, EmuBackend's pre_analysis counts params, but doesn't track their TAC Var.
            # We'll rely on the first actual use of the Var matching the param.
    
    # 1. Iterative Dataflow Analysis
    changed = True
    iterations = 0
    while changed and iterations < 100:
        changed = False
        iterations += 1
        
        for block in reversed(blocks):
            # new_live_out = union of live_in of successors
            new_live_out = set()
            for succ in block.successors:
                new_live_out.update(succ.live_in)
                
            block.live_out = new_live_out
            
            # backward scan to compute live_in
            current_live = set(block.live_out)
            for instr in reversed(block.instructions):
                # remove definitions
                if _defines_result(instr):
                    current_live.discard(instr.result)
                    
                # add uses
                if _is_value_operand(instr.arg1):
                    current_live.add(instr.arg1)
                if _is_value_operand(instr.arg2):
                    current_live.add(instr.arg2)
                
                # special case for ret returning a value
                if instr.op == "ret" and _is_value_operand(instr.arg1):
                    current_live.add(instr.arg1)
                    
            if current_live != block.live_in:
                block.live_in = current_live
                changed = True
                
    # 2. Build per-instruction live sets
    live_at = [set() for _ in range(line_count + 1)]
    
    for block in blocks:
        current_live = set(block.live_out)
        live_at[block.end_idx] = set(current_live)
        
        for i in range(block.end_idx - 1, block.start_idx - 1, -1):
            instr = instructions[i]
            
            if _defines_result(instr):
                current_live.discard(instr.result)
                
            if _is_value_operand(instr.arg1):
                current_live.add(instr.arg1)
            if _is_value_operand(instr.arg2):
                current_live.add(instr.arg2)
                
            if instr.op == "ret" and _is_value_operand(instr.arg1):
                current_live.add(instr.arg1)
                
            live_at[i] = set(current_live)
            
    # 3. Arg->Call Liveness Extension
    _extend_arg_liveness(instructions, live_at)
    
    # 4. Compute Intervals
    intervals = {}
    
    # Find all variables
    all_vars = set()
    for live_set in live_at:
        all_vars.update(live_set)
        
    for var in all_vars:
        first_def = -1
        last_use = -1
        
        # Is it a param? Params are alive at start.
        # Actually, if it's in a live_at set, find its bounds.
        for i in range(line_count + 1):
            if var in live_at[i]:
                if first_def == -1:
                    first_def = i
                last_use = i
        
        # If it's a function parameter, it must be alive at index 0.
        # A variable is a parameter if it's never explicitly defined before its first use.
        # Check if first_def is a use rather than a def.
        if first_def > 0:
            instr = instructions[first_def - 1]
            if not (_defines_result(instr) and instr.result == var):
                # It's used before it's defined! It must be a parameter.
                first_def = 0
                
        intervals[var] = (first_def, last_use)
        
    # Ensure intervals cover def to use.
    # Actually, intervals are built from live_at, which is already inclusive.
        
    # 5. Compute max simultaneous live
    max_live = max(len(s) for s in live_at) if live_at else 0
    
    return LivenessInfo(live_at, intervals, max_live)
