module Pages.LoanCalculator exposing (Model, Msg(..), init, update, view)

import Html exposing (Html, div, h1, h2, h3, p, input, label, button, text, table, tr, td, th, thead, tbody)
import Html.Attributes exposing (type_, value, placeholder, style, class, step)
import Html.Events exposing (onInput, onClick)
import String exposing (fromFloat, fromInt)
import Maybe exposing (withDefault)
import Basics exposing (min)

-- MODEL

type alias Model =
    { loanAmount : String
    , interestRate : String
    , loanTerm : String
    , monthlyPayment : Maybe Float
    , totalPayment : Maybe Float
    , totalInterest : Maybe Float
    , amortizationSchedule : List AmortizationRow
    }

type alias AmortizationRow =
    { paymentNumber : Int
    , payment : Float
    , principal : Float
    , interest : Float
    , remainingBalance : Float
    }

init : Model
init =
    { loanAmount = ""
    , interestRate = ""
    , loanTerm = ""
    , monthlyPayment = Nothing
    , totalPayment = Nothing
    , totalInterest = Nothing
    , amortizationSchedule = []
    }

-- UPDATE

type Msg
    = UpdateLoanAmount String
    | UpdateInterestRate String
    | UpdateLoanTerm String
    | Calculate

update : Msg -> Model -> Model
update msg model =
    case msg of
        UpdateLoanAmount value ->
            { model | loanAmount = value }

        UpdateInterestRate value ->
            { model | interestRate = value }

        UpdateLoanTerm value ->
            { model | loanTerm = value }

        Calculate ->
            calculateLoan model

-- VIEW

view : Model -> Html Msg
view model =
    div [ class "loan-calculator", style "max-width" "1200px", style "margin" "0 auto", style "padding" "20px" ]
        [ h1 [ style "text-align" "center", style "color" "#1976d2" ] [ text "Loan Payment Calculator" ]
        
        -- Input Form
        , div [ class "card", style "margin-bottom" "30px" ]
            [ h2 [] [ text "Loan Details" ]
            , div [ style "display" "grid", style "grid-template-columns" "repeat(auto-fit, minmax(250px, 1fr))", style "gap" "20px", style "margin-bottom" "20px" ]
                [ div []
                    [ label [ style "display" "block", style "margin-bottom" "5px", style "font-weight" "bold" ] [ text "Loan Amount ($)" ]
                    , input 
                        [ type_ "number"
                        , value model.loanAmount
                        , onInput UpdateLoanAmount
                        , placeholder "Enter loan amount"
                        , style "width" "100%"
                        , style "padding" "10px"
                        , style "border" "1px solid #ddd"
                        , style "border-radius" "4px"
                        , Html.Attributes.min "0"
                        , step "100"
                        ] []
                    ]
                , div []
                    [ label [ style "display" "block", style "margin-bottom" "5px", style "font-weight" "bold" ] [ text "Annual Interest Rate (%)" ]
                    , input 
                        [ type_ "number"
                        , value model.interestRate
                        , onInput UpdateInterestRate
                        , placeholder "Enter interest rate"
                        , style "width" "100%"
                        , style "padding" "10px"
                        , style "border" "1px solid #ddd"
                        , style "border-radius" "4px"
                        , Html.Attributes.min "0"
                        , step "0.1"
                        ] []
                    ]
                , div []
                    [ label [ style "display" "block", style "margin-bottom" "5px", style "font-weight" "bold" ] [ text "Loan Term (Years)" ]
                    , input 
                        [ type_ "number"
                        , value model.loanTerm
                        , onInput UpdateLoanTerm
                        , placeholder "Enter loan term"
                        , style "width" "100%"
                        , style "padding" "10px"
                        , style "border" "1px solid #ddd"
                        , style "border-radius" "4px"
                        , Html.Attributes.min "1"
                        , step "1"
                        ] []
                    ]
                ]
            , button 
                [ onClick Calculate
                , style "background" "#1976d2"
                , style "color" "white"
                , style "border" "none"
                , style "padding" "12px 24px"
                , style "border-radius" "4px"
                , style "cursor" "pointer"
                , style "font-size" "16px"
                ] 
                [ text "Calculate Payment" ]
            ]
        
        -- Results
        , case model.monthlyPayment of
            Just payment ->
                div [ class "card", style "margin-bottom" "30px" ]
                    [ h2 [] [ text "Payment Summary" ]
                    , div [ style "display" "grid", style "grid-template-columns" "repeat(auto-fit, minmax(200px, 1fr))", style "gap" "20px" ]
                        [ div [ style "text-align" "center", style "padding" "20px", style "background" "#f5f7fa", style "border-radius" "8px" ]
                            [ h3 [ style "margin" "0 0 10px 0", style "color" "#1976d2" ] [ text "Monthly Payment" ]
                            , p [ style "font-size" "24px", style "font-weight" "bold", style "margin" "0" ] [ text ("$" ++ fromFloat payment) ]
                            ]
                        , div [ style "text-align" "center", style "padding" "20px", style "background" "#f5f7fa", style "border-radius" "8px" ]
                            [ h3 [ style "margin" "0 0 10px 0", style "color" "#1976d2" ] [ text "Total Payment" ]
                            , p [ style "font-size" "24px", style "font-weight" "bold", style "margin" "0" ] 
                                [ text ("$" ++ fromFloat (withDefault 0 model.totalPayment)) ]
                            ]
                        , div [ style "text-align" "center", style "padding" "20px", style "background" "#f5f7fa", style "border-radius" "8px" ]
                            [ h3 [ style "margin" "0 0 10px 0", style "color" "#1976d2" ] [ text "Total Interest" ]
                            , p [ style "font-size" "24px", style "font-weight" "bold", style "margin" "0" ] 
                                [ text ("$" ++ fromFloat (withDefault 0 model.totalInterest)) ]
                            ]
                        ]
                    ]
            
            Nothing ->
                div [] []
        
        -- Amortization Schedule
        , if List.isEmpty model.amortizationSchedule then
            div [] []
          else
            div [ class "card" ]
                [ h2 [] [ text "Amortization Schedule" ]
                , div [ style "overflow-x" "auto" ]
                    [ table [ style "width" "100%", style "border-collapse" "collapse" ]
                        [ thead []
                            [ tr [ style "background" "#f5f7fa" ]
                                [ th [ style "padding" "12px", style "text-align" "left", style "border" "1px solid #ddd" ] [ text "Payment #" ]
                                , th [ style "padding" "12px", style "text-align" "left", style "border" "1px solid #ddd" ] [ text "Payment" ]
                                , th [ style "padding" "12px", style "text-align" "left", style "border" "1px solid #ddd" ] [ text "Principal" ]
                                , th [ style "padding" "12px", style "text-align" "left", style "border" "1px solid #ddd" ] [ text "Interest" ]
                                , th [ style "padding" "12px", style "text-align" "left", style "border" "1px solid #ddd" ] [ text "Remaining Balance" ]
                                ]
                            ]
                        , tbody [] (List.map amortizationRow model.amortizationSchedule)
                        ]
                    ]
                ]
        ]

amortizationRow : AmortizationRow -> Html msg
amortizationRow row =
    tr []
        [ td [ style "padding" "8px", style "border" "1px solid #ddd" ] [ text (fromInt row.paymentNumber) ]
        , td [ style "padding" "8px", style "border" "1px solid #ddd" ] [ text ("$" ++ fromFloat row.payment) ]
        , td [ style "padding" "8px", style "border" "1px solid #ddd" ] [ text ("$" ++ fromFloat row.principal) ]
        , td [ style "padding" "8px", style "border" "1px solid #ddd" ] [ text ("$" ++ fromFloat row.interest) ]
        , td [ style "padding" "8px", style "border" "1px solid #ddd" ] [ text ("$" ++ fromFloat row.remainingBalance) ]
        ]

-- CALCULATIONS

calculateLoan : Model -> Model
calculateLoan model =
    case ( String.toFloat model.loanAmount
         , String.toFloat model.interestRate
         , String.toFloat model.loanTerm
         ) of
        ( Just principal, Just rate, Just term ) ->
            let
                monthlyRate = rate / 100 / 12
                numberOfPayments = term * 12
                monthlyPayment = calculateMonthlyPayment principal monthlyRate numberOfPayments
                totalPayment = monthlyPayment * numberOfPayments
                totalInterest = totalPayment - principal
                schedule = generateAmortizationSchedule principal monthlyRate numberOfPayments monthlyPayment
            in
            { model 
            | monthlyPayment = Just monthlyPayment
            , totalPayment = Just totalPayment
            , totalInterest = Just totalInterest
            , amortizationSchedule = schedule
            }
        
        _ ->
            model

calculateMonthlyPayment : Float -> Float -> Float -> Float
calculateMonthlyPayment principal monthlyRate numberOfPayments =
    if monthlyRate == 0 then
        principal / numberOfPayments
    else
        principal * (monthlyRate * (1 + monthlyRate) ^ numberOfPayments) / ((1 + monthlyRate) ^ numberOfPayments - 1)

generateAmortizationSchedule : Float -> Float -> Float -> Float -> List AmortizationRow
generateAmortizationSchedule principal monthlyRate numberOfPayments monthlyPayment =
    let
        generateRow : Int -> Float -> List AmortizationRow -> List AmortizationRow
        generateRow paymentNumber remainingBalance acc =
            if toFloat paymentNumber > numberOfPayments || remainingBalance <= 0 then
                List.reverse acc
            else
                let
                    interest = remainingBalance * monthlyRate
                    principalPaid = monthlyPayment - interest
                    newBalance = remainingBalance - principalPaid
                    row = 
                        { paymentNumber = paymentNumber
                        , payment = monthlyPayment
                        , principal = principalPaid
                        , interest = interest
                        , remainingBalance = max 0 newBalance
                        }
                in
                generateRow (paymentNumber + 1) newBalance (row :: acc)
    in
    generateRow 1 principal [] 