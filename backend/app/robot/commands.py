"""Motion vocabulary shared by the policy, safety layer and transports.

Nothing above the transport layer ever builds raw device frames; it requests one
of these primitives and the safety controller decides whether it may run.
"""

from __future__ import annotations

import math
import uuid
from enum import StrEnum

from pydantic import BaseModel, Field, field_validator, model_validator


class MotionPrimitive(StrEnum):
    FORWARD = "forward"
    BACKWARD = "backward"
    STRAFE_LEFT = "strafe_left"
    STRAFE_RIGHT = "strafe_right"
    ROTATE_LEFT = "rotate_left"
    ROTATE_RIGHT = "rotate_right"
    STOP = "stop"


TRANSLATIONS = frozenset(
    {MotionPrimitive.FORWARD, MotionPrimitive.BACKWARD, MotionPrimitive.STRAFE_LEFT, MotionPrimitive.STRAFE_RIGHT}
)
ROTATIONS = frozenset({MotionPrimitive.ROTATE_LEFT, MotionPrimitive.ROTATE_RIGHT})

INVERSE = {
    MotionPrimitive.FORWARD: MotionPrimitive.BACKWARD,
    MotionPrimitive.BACKWARD: MotionPrimitive.FORWARD,
    MotionPrimitive.STRAFE_LEFT: MotionPrimitive.STRAFE_RIGHT,
    MotionPrimitive.STRAFE_RIGHT: MotionPrimitive.STRAFE_LEFT,
    MotionPrimitive.ROTATE_LEFT: MotionPrimitive.ROTATE_RIGHT,
    MotionPrimitive.ROTATE_RIGHT: MotionPrimitive.ROTATE_LEFT,
}


class MotionProfileName(StrEnum):
    LOW_SPEED_TEST = "LOW_SPEED_TEST"
    NORMAL_CONTROL = "NORMAL_CONTROL"
    SHORT_RETREAT = "SHORT_RETREAT"
    LINK_TEST = "LINK_TEST"


class MotionOrigin(StrEnum):
    POLICY = "policy"  # autonomous: the decision engine asked
    OPERATOR = "operator"  # dashboard manual control
    VOICE = "voice"  # a recognised high-confidence voice command
    RETURN = "return"  # controlled dead-reckoning return
    TEST = "test"  # bench test routines


AUTONOMOUS_ORIGINS = frozenset({MotionOrigin.POLICY})


class MotionProfile(BaseModel):
    name: MotionProfileName
    max_speed: int = Field(ge=0, le=100)
    max_duration_s: float = Field(gt=0, le=7.0)
    allowed: frozenset[MotionPrimitive]
    description: str = ""

    @model_validator(mode="after")
    def _short_unless_link_test(self) -> MotionProfile:
        # Only the R5 link test needs a window long enough to cover a frozen link.
        if self.name != MotionProfileName.LINK_TEST and self.max_duration_s > 3.0:
            raise ValueError("motion profiles other than LINK_TEST are limited to 3 s")
        return self


# Speeds are Zeus joystick radius units (0..100). These are deliberately conservative
# placeholders until wheels-off-ground and floor calibration (docs/TEST_PLAN.md R1-R3).
DEFAULT_PROFILES: dict[MotionProfileName, MotionProfile] = {
    MotionProfileName.LOW_SPEED_TEST: MotionProfile(
        name=MotionProfileName.LOW_SPEED_TEST,
        max_speed=45,
        max_duration_s=0.5,
        allowed=frozenset(TRANSLATIONS | ROTATIONS),
        description="Bench and first-floor tests. Wheels off the ground first.",
    ),
    MotionProfileName.NORMAL_CONTROL: MotionProfile(
        name=MotionProfileName.NORMAL_CONTROL,
        max_speed=70,
        max_duration_s=1.0,
        allowed=frozenset(TRANSLATIONS | ROTATIONS),
        description="Operator jog control from the dashboard.",
    ),
    MotionProfileName.SHORT_RETREAT: MotionProfile(
        name=MotionProfileName.SHORT_RETREAT,
        max_speed=60,
        max_duration_s=0.8,
        # No rotation for autonomous use: on the stock Zeus firmware rotation is
        # closed-loop on the magnetometer, which phones and laptops disturb.
        allowed=frozenset(TRANSLATIONS),
        description="Autonomous retreat away from a reaching hand.",
    ),
    MotionProfileName.LINK_TEST: MotionProfile(
        name=MotionProfileName.LINK_TEST,
        max_speed=40,
        # lead (<= 1 s) + frozen link (<= 5 s) + margin; the runtime ends it with stop().
        max_duration_s=7.0,
        allowed=frozenset(TRANSLATIONS),
        description="R5 link-freeze test only (origin test). Wheels off the ground first.",
    ),
}


class MotionRequest(BaseModel):
    primitive: MotionPrimitive
    speed: float | None = None
    duration_s: float | None = None
    profile: MotionProfileName = MotionProfileName.NORMAL_CONTROL
    origin: MotionOrigin = MotionOrigin.OPERATOR
    reason: str = ""
    request_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])

    @field_validator("speed", "duration_s")
    @classmethod
    def _finite(cls, v: float | None) -> float | None:
        if v is not None and (math.isnan(v) or math.isinf(v)):
            raise ValueError("must be a finite number")
        return v


class ApprovedMotion(BaseModel):
    request_id: str
    primitive: MotionPrimitive
    speed: int
    duration_s: float
    profile: MotionProfileName
    origin: MotionOrigin
    reason: str
    clamped: list[str] = Field(default_factory=list)
