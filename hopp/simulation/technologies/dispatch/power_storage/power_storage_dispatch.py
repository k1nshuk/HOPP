import numpy as np
import pyomo.environ as pyomo
from pyomo.network import Port
from pyomo.environ import units as u

from hopp.simulation.technologies.dispatch.dispatch import Dispatch


class PowerStorageDispatch(Dispatch):
    """Dispatch algorithm for power storage."""

    def __init__(
        self,
        pyomo_model: pyomo.ConcreteModel,
        index_set: pyomo.Set,
        system_model,
        financial_model,
        block_set_name: str,
        dispatch_options,
    ):
        """Initialize PowerStorageDispatch.

        Args:
            pyomo_model (pyomo.ConcreteModel): Pyomo concrete model.
            index_set (pyomo.Set): Indexed set.
            system_model: System model.
            financial_model: Financial model.
            block_set_name (str, optional): Name of the block set.
            dispatch_options (dict, optional): Dispatch options.

        """

        super().__init__(
            pyomo_model,
            index_set,
            system_model,
            financial_model,
            block_set_name=block_set_name,
        )
        self._create_soc_linking_constraint()

        # TODO: we could remove this option and just have lifecycle count default
        self.options = dispatch_options
        if self.options.include_lifecycle_count:
            self._create_lifecycle_model()
            if self.options.max_lifecycle_per_day < np.inf:
                self._create_lifecycle_count_constraint()

    def dispatch_block_rule(self, storage):
        """Initializes storage parameters, variables, and constraints.
            Called during Dispatch's __init__.

        Args:
            storage: Storage instance.

        """
        # Parameters
        self._create_storage_parameters(storage)
        self._create_efficiency_parameters(storage)
        self._create_capacity_parameter(storage)
        # Variables
        self._create_storage_variables(storage)
        # Constraints
        self._create_storage_constraints(storage)
        self._create_soc_inventory_constraint(storage)
        # Ports
        self._create_storage_port(storage)

    def max_gross_profit_objective(self, hybrid_blocks):
        """Sets the max gross profit objective for the dispatch.

        Args:
            hybrid_blocks (Pyomo.block): A generalized container for defining hierarchical
                models by adding modeling components as attributes.

        """

        def battery_profit_objective_rule(m):
            objective = 0
            objective += sum(
                -(1 / hybrid_blocks[t].time_weighting_factor)
                * self.blocks[t].time_duration
                * (
                    self.blocks[t].cost_per_charge * hybrid_blocks[t].battery_charge
                    + self.blocks[t].cost_per_discharge
                    * hybrid_blocks[t].battery_discharge
                )
                for t in hybrid_blocks.index_set()
            )
            if self.options.include_lifecycle_count:
                objective -= self.model.lifecycle_cost * sum(self.model.lifecycles)
            return objective

        self.obj = pyomo.Expression(rule=battery_profit_objective_rule)

    def min_operating_cost_objective(self, hybrid_blocks):
        """Sets the min operating cost objective for the dispatch.

        Args:
            hybrid_blocks (Pyomo.block): A generalized container for defining hierarchical
                models by adding modeling components as attributes.

        """
        objective = sum(
            hybrid_blocks[t].time_weighting_factor
            * self.blocks[t].time_duration
            * (
                self.blocks[t].cost_per_discharge * hybrid_blocks[t].battery_discharge
                - self.blocks[t].cost_per_charge * hybrid_blocks[t].battery_charge
            )  # Try to incentivize battery charging
            for t in self.blocks.index_set()
        )
        if self.options.include_lifecycle_count:
            objective += self.model.lifecycle_cost * sum(self.model.lifecycles)

        self.obj = objective

    def _create_variables(self, hybrid):
        """Creates storage variables.

        Args:
            hybrid: Hybrid instance.

        Returns:
            Tuple: Tuple containing battery discharge and charge variables.

        """
        hybrid.battery_charge = pyomo.Var(
            doc="Power charging the electric battery [MW]",
            domain=pyomo.NonNegativeReals,
            units=u.MW,
            initialize=0.0,
        )
        hybrid.battery_discharge = pyomo.Var(
            doc="Power discharging the electric battery [MW]",
            domain=pyomo.NonNegativeReals,
            units=u.MW,
            initialize=0.0,
        )
        return hybrid.battery_discharge, hybrid.battery_charge

    def _create_port(self, hybrid):
        """Creates storage port.

        Args:
            hybrid: Hybrid instance.

        Returns:
            Port: Storage port.

        """
        hybrid.battery_port = Port(
            initialize={
                "charge_power": hybrid.battery_charge,
                "discharge_power": hybrid.battery_discharge,
            }
        )
        return hybrid.battery_port

    def _create_storage_parameters(self, storage):
        """Creates storage parameters.

        Args:
            storage: Storage instance.

        """
        ##################################
        # Parameters                     #
        ##################################
        storage.time_duration = pyomo.Param(
            doc="Time step [hour]",
            default=1.0,
            within=pyomo.NonNegativeReals,
            mutable=True,
            units=u.hr,
        )
        storage.cost_per_charge = pyomo.Param(
            doc="Operating cost of " + self.block_set_name + " charging [$/MWh]",
            default=0.0,
            within=pyomo.NonNegativeReals,
            mutable=True,
            units=u.USD / u.MWh,
        )
        storage.cost_per_discharge = pyomo.Param(
            doc="Operating cost of " + self.block_set_name + " discharging [$/MWh]",
            default=0.0,
            within=pyomo.NonNegativeReals,
            mutable=True,
            units=u.USD / u.MWh,
        )
        storage.minimum_power = pyomo.Param(
            doc=self.block_set_name + " minimum power rating [MW]",
            default=0.0,
            within=pyomo.NonNegativeReals,
            mutable=True,
            units=u.MW,
        )
        storage.maximum_power = pyomo.Param(
            doc=self.block_set_name + " maximum power rating [MW]",
            within=pyomo.NonNegativeReals,
            mutable=True,
            units=u.MW,
        )
        storage.minimum_soc = pyomo.Param(
            doc=self.block_set_name + " minimum state-of-charge [-]",
            default=0.1,
            within=pyomo.PercentFraction,
            mutable=True,
            units=u.dimensionless,
        )
        storage.maximum_soc = pyomo.Param(
            doc=self.block_set_name + " maximum state-of-charge [-]",
            default=0.9,
            within=pyomo.PercentFraction,
            mutable=True,
            units=u.dimensionless,
        )

    def _create_efficiency_parameters(self, storage):
        """Creates storage efficiency parameters.

        Args:
            storage: Storage instance.

        """
        storage.charge_efficiency = pyomo.Param(
            doc=self.block_set_name + " Charging efficiency [-]",
            default=0.938,
            within=pyomo.PercentFraction,
            mutable=True,
            units=u.dimensionless,
        )
        storage.discharge_efficiency = pyomo.Param(
            doc=self.block_set_name + " discharging efficiency [-]",
            default=0.938,
            within=pyomo.PercentFraction,
            mutable=True,
            units=u.dimensionless,
        )

    def _create_capacity_parameter(self, storage):
        """Creates storage capacity parameter.

        Args:
            storage: Storage instance.

        """
        storage.capacity = pyomo.Param(
            doc=self.block_set_name + " capacity [MWh]",
            within=pyomo.NonNegativeReals,
            mutable=True,
            units=u.MWh,
        )

    def _create_storage_variables(self, storage):
        """Creates storage variables.

        Args:
            storage: Storage instance.

        """
        ##################################
        # Variables                      #
        ##################################
        storage.is_charging = pyomo.Var(
            doc="1 if " + self.block_set_name + " is charging; 0 Otherwise [-]",
            domain=pyomo.Binary,
            units=u.dimensionless,
        )
        storage.is_discharging = pyomo.Var(
            doc="1 if " + self.block_set_name + " is discharging; 0 Otherwise [-]",
            domain=pyomo.Binary,
            units=u.dimensionless,
        )
        storage.soc0 = pyomo.Var(
            doc=self.block_set_name
            + " initial state-of-charge at beginning of period[-]",
            domain=pyomo.PercentFraction,
            bounds=(storage.minimum_soc, storage.maximum_soc),
            units=u.dimensionless,
        )
        storage.soc = pyomo.Var(
            doc=self.block_set_name + " state-of-charge at end of period [-]",
            domain=pyomo.PercentFraction,
            bounds=(storage.minimum_soc, storage.maximum_soc),
            units=u.dimensionless,
        )
        storage.charge_power = pyomo.Var(
            doc="Power into " + self.block_set_name + " [MW]",
            domain=pyomo.NonNegativeReals,
            units=u.MW,
        )
        storage.discharge_power = pyomo.Var(
            doc="Power out of " + self.block_set_name + " [MW]",
            domain=pyomo.NonNegativeReals,
            units=u.MW,
        )

    def _create_storage_constraints(self, storage):
        ##################################
        # Constraints                    #
        ##################################
        # Charge power bounds
        storage.charge_power_ub = pyomo.Constraint(
            doc=self.block_set_name + " charging power upper bound",
            expr=storage.charge_power <= storage.maximum_power * storage.is_charging,
        )
        storage.charge_power_lb = pyomo.Constraint(
            doc=self.block_set_name + " charging power lower bound",
            expr=storage.charge_power >= storage.minimum_power * storage.is_charging,
        )
        # Discharge power bounds
        storage.discharge_power_lb = pyomo.Constraint(
            doc=self.block_set_name + " Discharging power lower bound",
            expr=storage.discharge_power
            >= storage.minimum_power * storage.is_discharging,
        )
        storage.discharge_power_ub = pyomo.Constraint(
            doc=self.block_set_name + " Discharging power upper bound",
            expr=storage.discharge_power
            <= storage.maximum_power * storage.is_discharging,
        )
        # Storage packing constraint
        storage.charge_discharge_packing = pyomo.Constraint(
            doc=self.block_set_name
            + " packing constraint for charging and discharging binaries",
            expr=storage.is_charging + storage.is_discharging <= 1,
        )

    def _create_soc_inventory_constraint(self, storage):
        """Creates state-of-charge inventory constraint for storage.

        Args:
            storage: Storage instance.

        """

        def soc_inventory_rule(m):
            return m.soc == (
                m.soc0
                + m.time_duration
                * (
                    m.charge_efficiency * m.charge_power
                    - (1 / m.discharge_efficiency) * m.discharge_power
                )
                / m.capacity
            )

        # Storage State-of-charge balance
        storage.soc_inventory = pyomo.Constraint(
            doc=self.block_set_name + " state-of-charge inventory balance",
            rule=soc_inventory_rule,
        )

    @staticmethod
    def _create_storage_port(storage):
        """Creates storage port.

        Args:
            storage: Storage instance.

        """
        ##################################
        # Ports                          #
        ##################################
        storage.port = Port()
        storage.port.add(storage.charge_power)
        storage.port.add(storage.discharge_power)

    def _create_soc_linking_constraint(self):
        """Creates state-of-charge linking constraint."""
        ##################################
        # Parameters                     #
        ##################################
        self.model.initial_soc = pyomo.Param(
            doc=self.block_set_name
            + " initial state-of-charge at beginning of the horizon[-]",
            within=pyomo.PercentFraction,
            default=0.5,
            mutable=True,
            units=u.dimensionless,
        )
        ##################################
        # Constraints                    #
        ##################################

        # Linking time periods together
        def storage_soc_linking_rule(m, t):
            if t == self.blocks.index_set().first():
                return self.blocks[t].soc0 == self.model.initial_soc
            return self.blocks[t].soc0 == self.blocks[t - 1].soc

        self.model.soc_linking = pyomo.Constraint(
            self.blocks.index_set(),
            doc=self.block_set_name + " state-of-charge block linking constraint",
            rule=storage_soc_linking_rule,
        )

    def _lifecycle_count_rule(self, m, i):
        """Calculates lifecycle count rule.

        Args:
            m: Model instance.
            i: Index.

        Returns:
            float: Lifecycle count.

        """
        # Use full-energy cycles
        start = int(i * self.timesteps_per_day)
        end = int((i + 1) * self.timesteps_per_day)
        return m.lifecycles[i] == sum(
            self.blocks[t].time_duration
            * self.blocks[t].discharge_power
            / self.blocks[t].capacity
            for t in range(start, end)
        )

    def _create_lifecycle_model(self):
        """Creates lifecycle model."""
        ##################################
        # Parameters                     #
        ##################################
        self.timesteps_per_day = 24 / pyomo.value(self.blocks[0].time_duration)
        self.model.days = pyomo.RangeSet(
            0, int(len(self.blocks)) / self.timesteps_per_day - 1
        )
        self.model.lifecycle_cost = pyomo.Param(
            doc="Lifecycle cost of " + self.block_set_name + " [$/lifecycle]",
            default=0.0,
            within=pyomo.NonNegativeReals,
            mutable=True,
            units=u.USD / u.lifecycle,
        )
        ##################################
        # Variables                      #
        ##################################
        self.model.lifecycles = pyomo.Var(
            self.model.days,
            doc=self.block_set_name + " lifecycle count",
            domain=pyomo.NonNegativeReals,
            units=u.lifecycle,
        )
        ##################################
        # Constraints                    #
        ##################################
        self.model.lifecycle_count = pyomo.Constraint(
            self.model.days,
            doc=self.block_set_name + " lifecycle counting",
            rule=self._lifecycle_count_rule,
        )
        ##################################
        # Ports                          #
        ##################################
        self.model.lifecycles_port = Port()
        self.model.lifecycles_port.add(self.model.lifecycles)
        self.model.lifecycles_port.add(self.model.lifecycle_cost)

    def _create_lifecycle_count_constraint(self):
        """Creates lifecycle count constraint."""
        self.model.max_cycles_per_day = pyomo.Param(
            doc="Max number of full energy cycles per day for " + self.block_set_name,
            default=self.options.max_lifecycle_per_day,
            within=pyomo.NonNegativeReals,
            mutable=True,
            units=u.lifecycle,
        )

        self.model.lifecycle_count_constraint = pyomo.Constraint(
            self.model.days, rule=lambda m, i: m.lifecycles[i] <= m.max_cycles_per_day
        )

    def _check_initial_soc(self, initial_soc):
        """Checks initial state-of-charge.

        Args:
            initial_soc: Initial state-of-charge value.

        Returns:
            float: Checked initial state-of-charge.
            
        """
        if initial_soc > 1:
            initial_soc /= 100.0
        initial_soc = round(initial_soc, self.round_digits)
        if initial_soc > self.maximum_soc / 100:
            print(
                "Warning: Storage dispatch was initialized with a state-of-charge greater than "
                "maximum value!"
            )
            print("Initial SOC = {}".format(initial_soc))
            print("Initial SOC was set to maximum value.")
            initial_soc = self.maximum_soc / 100
        elif initial_soc < self.minimum_soc / 100:
            print(
                "Warning: Storage dispatch was initialized with a state-of-charge less than "
                "minimum value!"
            )
            print("Initial SOC = {}".format(initial_soc))
            print("Initial SOC was set to minimum value.")
            initial_soc = self.minimum_soc / 100
        return initial_soc

    def update_dispatch_initial_soc(self, initial_soc: float = None):
        raise NotImplemented(
            "This function must be overridden for specific storage dispatch model"
        )

    # INPUTS
    @property
    def time_duration(self) -> list:
        """Time duration."""
        return [self.blocks[t].time_duration.value for t in self.blocks.index_set()]

    @time_duration.setter
    def time_duration(self, time_duration: list):
        if len(time_duration) == len(self.blocks):
            for t, delta in zip(self.blocks, time_duration):
                self.blocks[t].time_duration = round(delta, self.round_digits)
        else:
            raise ValueError(
                self.time_duration.__name__
                + " list must be the same length as time horizon"
            )

    @property
    def cost_per_charge(self) -> float:
        """Cost per charge."""
        for t in self.blocks.index_set():
            return self.blocks[t].cost_per_charge.value

    @cost_per_charge.setter
    def cost_per_charge(self, om_dollar_per_mwh: float):
        for t in self.blocks.index_set():
            self.blocks[t].cost_per_charge = round(om_dollar_per_mwh, self.round_digits)

    @property
    def cost_per_discharge(self) -> float:
        """Cost per discharge."""
        for t in self.blocks.index_set():
            return self.blocks[t].cost_per_discharge.value

    @cost_per_discharge.setter
    def cost_per_discharge(self, om_dollar_per_mwh: float):
        for t in self.blocks.index_set():
            self.blocks[t].cost_per_discharge = round(
                om_dollar_per_mwh, self.round_digits
            )

    @property
    def minimum_power(self) -> float:
        """Minimum power."""
        for t in self.blocks.index_set():
            return self.blocks[t].minimum_power.value

    @minimum_power.setter
    def minimum_power(self, minimum_power_mw: float):
        for t in self.blocks.index_set():
            self.blocks[t].minimum_power = round(minimum_power_mw, self.round_digits)

    @property
    def maximum_power(self) -> float:
        """Maximum power."""
        for t in self.blocks.index_set():
            return self.blocks[t].maximum_power.value

    @maximum_power.setter
    def maximum_power(self, maximum_power_mw: float):
        for t in self.blocks.index_set():
            self.blocks[t].maximum_power = round(maximum_power_mw, self.round_digits)

    @property
    def minimum_soc(self) -> float:
        """Minimum state-of-charge."""
        for t in self.blocks.index_set():
            return self.blocks[t].minimum_soc.value * 100.0

    @minimum_soc.setter
    def minimum_soc(self, minimum_soc: float):
        if minimum_soc > 1:
            minimum_soc /= 100.0
        for t in self.blocks.index_set():
            self.blocks[t].minimum_soc = round(minimum_soc, self.round_digits)

    @property
    def maximum_soc(self) -> float:
        """Maximum state-of-charge."""
        for t in self.blocks.index_set():
            return self.blocks[t].maximum_soc.value * 100.0

    @maximum_soc.setter
    def maximum_soc(self, maximum_soc: float):
        if maximum_soc > 1:
            maximum_soc /= 100.0
        for t in self.blocks.index_set():
            self.blocks[t].maximum_soc = round(maximum_soc, self.round_digits)

    @property
    def charge_efficiency(self) -> float:
        """Charge efficiency."""
        for t in self.blocks.index_set():
            return self.blocks[t].charge_efficiency.value * 100.0

    @charge_efficiency.setter
    def charge_efficiency(self, efficiency: float):
        efficiency = self._check_efficiency_value(efficiency)
        for t in self.blocks.index_set():
            self.blocks[t].charge_efficiency = round(efficiency, self.round_digits)

    @property
    def discharge_efficiency(self) -> float:
        """Discharge efficiency."""
        for t in self.blocks.index_set():
            return self.blocks[t].discharge_efficiency.value * 100.0

    @discharge_efficiency.setter
    def discharge_efficiency(self, efficiency: float):
        efficiency = self._check_efficiency_value(efficiency)
        for t in self.blocks.index_set():
            self.blocks[t].discharge_efficiency = round(efficiency, self.round_digits)

    @property
    def round_trip_efficiency(self) -> float:
        """Round trip efficiency."""
        return self.charge_efficiency * self.discharge_efficiency / 100.0

    @round_trip_efficiency.setter
    def round_trip_efficiency(self, round_trip_efficiency: float):
        round_trip_efficiency = self._check_efficiency_value(round_trip_efficiency)
        # Assumes equal charge and discharge efficiencies
        efficiency = round_trip_efficiency ** (1 / 2)
        self.charge_efficiency = efficiency
        self.discharge_efficiency = efficiency

    @property
    def capacity(self) -> float:
        """Capacity."""
        for t in self.blocks.index_set():
            return self.blocks[t].capacity.value

    @capacity.setter
    def capacity(self, capacity_mwh: float):
        for t in self.blocks.index_set():
            self.blocks[t].capacity = round(capacity_mwh, self.round_digits)

    @property
    def initial_soc(self) -> float:
        """Initial state-of-charge."""
        return self.model.initial_soc.value * 100.0

    @initial_soc.setter
    def initial_soc(self, initial_soc: float):
        initial_soc = self._check_initial_soc(initial_soc)
        self.model.initial_soc = round(initial_soc, self.round_digits)

    @property
    def lifecycle_cost(self) -> float:
        """Lifecycle cost."""
        return self.model.lifecycle_cost.value

    @lifecycle_cost.setter
    def lifecycle_cost(self, lifecycle_cost: float):
        self.model.lifecycle_cost = lifecycle_cost

    @property
    def lifecycle_cost_per_kWh_cycle(self) -> float:
        """Lifecycle cost per kWh cycle."""
        return self.options.lifecycle_cost_per_kWh_cycle

    @lifecycle_cost_per_kWh_cycle.setter
    def lifecycle_cost_per_kWh_cycle(self, lifecycle_cost_per_kWh_cycle: float):
        self.options.lifecycle_cost_per_kWh_cycle = lifecycle_cost_per_kWh_cycle
        self.model.lifecycle_cost = (
            lifecycle_cost_per_kWh_cycle * self._system_model.value("nominal_energy")
        )

    # Outputs
    @property
    def is_charging(self) -> list:
        """Storage is charging."""
        return [self.blocks[t].is_charging.value for t in self.blocks.index_set()]

    @property
    def is_discharging(self) -> list:
        """Storage is discharging."""
        return [self.blocks[t].is_discharging.value for t in self.blocks.index_set()]

    @property
    def soc(self) -> list:
        """State-of-charge."""
        return [self.blocks[t].soc.value * 100.0 for t in self.blocks.index_set()]

    @property
    def charge_power(self) -> list:
        """Charge power."""
        return [self.blocks[t].charge_power.value for t in self.blocks.index_set()]

    @property
    def discharge_power(self) -> list:
        """Discharge power."""
        return [self.blocks[t].discharge_power.value for t in self.blocks.index_set()]

    @property
    def lifecycles(self) -> float:
        """Lifecycles."""
        if self.options.include_lifecycle_count:
            return [pyomo.value(i) for _, i in self.model.lifecycles.items()]
        else:
            return []

    @property
    def power(self) -> list:
        """Power."""
        return [
            self.blocks[t].discharge_power.value - self.blocks[t].charge_power.value
            for t in self.blocks.index_set()
        ]

    @property
    def current(self) -> list:
        """Current."""
        return [0.0 for t in self.blocks.index_set()]

    @property
    def generation(self) -> list:
        """Generation."""
        return self.power
