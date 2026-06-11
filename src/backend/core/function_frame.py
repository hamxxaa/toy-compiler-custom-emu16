from dataclasses import dataclass
from typing import List
from .liveness import LivenessInfo
from .allocator import LocationMap, Location
from codegen.TACGenerator import Const

@dataclass
class FunctionFrame:
    name: str
    param_count: int
    param_names: List[str]
    location_map: LocationMap
    liveness: LivenessInfo
    callee_saved: List[int]
    frame_size: int
    epilogue_label: str
    is_naked: bool = False

    def loc(self, operand) -> Location:
        """Look up the pre-computed location for any operand."""
        if isinstance(operand, Const):
            raise ValueError("loc() called on a Const operand. Consts do not have allocated locations.")
        
        # Operand must be in location_map
        if operand not in self.location_map.locations:
            # Let's try matching by name and type
            # (sometimes the AST passes a different instance of Var with same properties)
            for var, location in self.location_map.locations.items():
                if var == operand:
                    return location
            raise ValueError(f"Operand {operand} not found in location map for function {self.name}")
            
        return self.location_map.locations[operand]
