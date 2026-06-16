from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple
from .liveness import LivenessInfo

@dataclass
class Location:
    kind: str           # "register" or "stack"
    register: Optional[int] = None      # 1-5 if kind=="register", else None
    fp_offset: Optional[int] = None     # FP-relative offset if kind=="stack", else None

    def __str__(self):
        if self.kind == "register":
            return f"R{self.register}"
        return f"[FP{self.fp_offset:+d}]"

@dataclass
class LocationMap:
    locations: Dict[any, Location]
    callee_saved: List[int]             # subset of [4, 5]
    frame_size: int                     # bytes to SUB from SP
    # Address-taken params that arrived in R1-R3 but were forced to a stack slot: the prologue
    # must spill the incoming register into the slot. List of (incoming_register, fp_offset).
    spilled_params: List[Tuple[int, int]] = field(default_factory=list)

def allocate_function(liveness_info: LivenessInfo, param_count: int, param_names: List[str],
                      address_taken: set = None, verbose: bool = False) -> LocationMap:
    """
    Assign a fixed Location (register or stack) to every variable in the function.

    address_taken: set of (name, scope_id) tuples for variables whose address was taken
                   with &x. Those variables are forced to a stack slot so their address is
                   a stable FP-relative pointer throughout the function.
    """
    locations: Dict[any, Location] = {}

    occupied_until: Dict[int, int] = {}  # reg_num -> last_use_idx

    param_vars = []
    # Sort variables by start interval
    sorted_intervals = sorted(liveness_info.intervals.items(), key=lambda item: item[1][0])

    # Globals are NOT register-allocated: they have a fixed memory home (data section).
    # Arrays (type ends with "[]") are also excluded — their identity IS their address.
    sorted_intervals = [
        (v, iv) for (v, iv) in sorted_intervals
        if getattr(v, "storage", None) != "global"
        and not getattr(v, "type", "").endswith("[]")
    ]

    # Helper: check whether a Var is in the address_taken set.
    _at = address_taken or set()
    def _is_addr_taken(var):
        if not _at or not hasattr(var, 'name') or not hasattr(var, 'scope_id'):
            return False
        base = var.name.split("_s")[0] if "_s" in var.name else var.name
        return (base, var.scope_id) in _at
    
    # Filter out parameters based on name (assuming Var has .name attribute)
    # The param names are e.g., 'x', 'y'.
    param_names_clean = [p.split(":")[0] if isinstance(p, str) and ":" in p else str(p).split(":")[0] for p in param_names]
    param_set = set(param_names_clean)
    
    for var, (start, end) in sorted_intervals:
        if hasattr(var, "name") and hasattr(var, "storage") and var.storage == "local":
            base_name = var.name.split("_s")[0] if "_s" in var.name else var.name
            if base_name in param_set:
                param_vars.append(var)
    
    # Sort param_vars to match the order in param_names
    # If param_vars doesn't have all params (maybe some are never used), we still must reserve their registers.
    # Actually, the caller ABI puts param[i] in R[i+1].
    for i, p_name in enumerate(param_names_clean):
        if i >= 3:
            # param 3+ are passed on the stack by the caller
            # Caller pushes them before the call.
            # FP points to return address.
            # FP + 2 points to saved FP.
            # FP + 4 is param 3 (4th param)
            # FP + 6 is param 4 (5th param)
            # We must assign them stack locations with positive FP offsets.
            # Let's find the matching var
            var_match = next((v for v in param_vars if (v.name.split("_s")[0] if "_s" in v.name else v.name) == p_name), None)
            if var_match:
                fp_offset = 4 + (i - 3) * 2
                locations[var_match] = Location(kind="stack", fp_offset=fp_offset)
            continue
            
        reg_num = i + 1
        var_match = next((v for v in param_vars if (v.name.split("_s")[0] if "_s" in v.name else v.name) == p_name), None)
        if var_match:
            locations[var_match] = Location(kind="register", register=reg_num)
            # The register is occupied until the param's last use
            occupied_until[reg_num] = liveness_info.intervals[var_match][1]
        else:
            # Param not used in function? We don't allocate a var, but register is free from start.
            occupied_until[reg_num] = 0

    # Slot/scan bookkeeping — initialized BEFORE the address-taken-param loop so that loop can
    # carve out stack slots (it runs before the linear scan). All slot consumers share one counter.
    free_regs = [1, 2, 3, 4, 5]
    active_regs: List[Tuple[int, int]] = []      # (end_idx, reg_num)
    active_spills: List[Tuple[int, int]] = []    # (end_idx, slot_idx)
    free_slots: List[int] = []
    next_slot_idx = 0
    callee_saved_used = set()                    # which callee-saved regs we actually used

    # 1b. Force address-taken params onto the stack instead of a register.  Their caller-side
    #     value is still in R1/R2/R3 on entry, so record (incoming_reg, var); the prologue must
    #     STROFF that register into the slot (resolved to a final fp_offset below).
    spilled_param_vars = []  # [(incoming_register, var)]
    for var in list(param_vars):
        if _is_addr_taken(var) and var in locations and locations[var].kind == "register":
            # Reclassify: free the register slot and give it a stack slot.
            reg = locations[var].register
            occupied_until[reg] = 0  # register is free again
            slot = next_slot_idx
            next_slot_idx += 1
            locations[var] = Location(kind="stack", fp_offset=-slot)
            spilled_param_vars.append((reg, var))

    # 2. Allocate remaining variables (Linear Scan)
    
    # Pre-pass: force all address-taken locals to a stack slot BEFORE linear scan.
    # This guarantees their FP-relative address is valid for the whole function.
    # We deliberately do NOT add them to active_spills so the slot is never recycled
    # (the address must remain stable for as long as the pointer could be used).
    for var, (start, end) in sorted_intervals:
        if var in locations:
            continue  # already allocated (param)
        if _is_addr_taken(var):
            slot = next_slot_idx
            next_slot_idx += 1
            locations[var] = Location(kind="stack", fp_offset=-slot)

    for var, (start, end) in sorted_intervals:
        if var in locations:
            # Already allocated (param or address-taken pre-pass)
            loc = locations[var]
            if loc.kind == "register":
                active_regs.append((end, loc.register))
            continue
            
        # Expire old intervals
        # Free registers
        new_active_regs = []
        for a_end, a_reg in active_regs:
            if a_end < start:
                if a_reg not in free_regs:
                    free_regs.append(a_reg)
            else:
                new_active_regs.append((a_end, a_reg))
        active_regs = new_active_regs
        
        # Free stack slots
        new_active_spills = []
        for a_end, a_slot in active_spills:
            if a_end < start:
                free_slots.append(a_slot)
            else:
                new_active_spills.append((a_end, a_slot))
        active_spills = new_active_spills
        
        # Check if we can allocate a register
        # We also need to check occupied_until for R1-R3 which might be held by unused params
        available_regs = [r for r in free_regs if start > occupied_until.get(r, -1)]
        
        if available_regs:
            # Pick a register. Prefer R4/R5 if the variable lives a long time or crosses calls?
            # For simplicity, just pick the first available. We'll pick R4/R5 first if available
            # so we leave R1-R3 free for short-lived things, or vice versa?
            # Prefer callee-saved if it's going to be used anyway?
            # Let's just sort available_regs. R4/R5 are 4, 5. 
            available_regs.sort()
            reg = available_regs[0]
            free_regs.remove(reg)
            locations[var] = Location(kind="register", register=reg)
            active_regs.append((end, reg))
            if reg in (4, 5):
                callee_saved_used.add(reg)
        else:
            # Spill to stack
            if free_slots:
                # Reuse slot
                free_slots.sort()
                slot = free_slots.pop(0)
            else:
                slot = next_slot_idx
                next_slot_idx += 1
            
            # We don't know the exact FP offset yet because it depends on how many callee-saved registers we push.
            # We will store the slot index temporarily.
            locations[var] = Location(kind="stack", fp_offset=-slot) # use negative as a placeholder marker
            active_spills.append((end, slot))
            
    # 3. Compute final FP offsets for spilled variables
    callee_saved_list = sorted(list(callee_saved_used))
    num_callee_saved = len(callee_saved_list)
    
    # Layout below FP:
    # FP - 2 : saved R4 (if used)
    # FP - 4 : saved R5 (if used)
    # FP - (num_callee_saved*2 + 2) : slot 0
    # FP - (num_callee_saved*2 + 4) : slot 1
    
    base_spill_offset = num_callee_saved * 2
    
    for var, loc in locations.items():
        if loc.kind == "stack" and loc.fp_offset is not None and loc.fp_offset <= 0:
            slot_idx = -loc.fp_offset
            real_fp_offset = -(base_spill_offset + (slot_idx * 2) + 2)
            loc.fp_offset = real_fp_offset

    frame_size = next_slot_idx * 2

    # Address-taken register-params: resolve each slot to its final FP offset now that the
    # placeholders above have been converted. The prologue spills R<reg> -> [FP + fp_offset].
    spilled_params = [(reg, locations[var].fp_offset) for (reg, var) in spilled_param_vars]

    if verbose:
        print(f"ALLOCATOR: func param_names={param_names}")
        for k, v in locations.items():
            print(f"  {getattr(k, 'name', k)} -> {v}")

    return LocationMap(locations, callee_saved_list, frame_size, spilled_params)
