module Pages.Register exposing (Model, Msg(..), init, update, view)

import Html exposing (Html, div, h1, label, input, button, text, form, p, a)
import Html.Attributes exposing (type_, value, placeholder, class, style, for, name, id, required, href)
import Html.Events exposing (onInput, onClick, onSubmit)
import String

-- MODEL

type alias Model =
    { name : String
    , email : String
    , password : String
    , confirmPassword : String
    , error : Maybe String
    }

init : Model
init =
    { name = ""
    , email = ""
    , password = ""
    , confirmPassword = ""
    , error = Nothing
    }

-- UPDATE

type Msg
    = UpdateName String
    | UpdateEmail String
    | UpdatePassword String
    | UpdateConfirmPassword String
    | Submit
    | ClearError

update : Msg -> Model -> (Model, Cmd Msg)
update msg model =
    case msg of
        UpdateName v -> ({ model | name = v, error = Nothing }, Cmd.none)
        UpdateEmail v -> ({ model | email = v, error = Nothing }, Cmd.none)
        UpdatePassword v -> ({ model | password = v, error = Nothing }, Cmd.none)
        UpdateConfirmPassword v -> ({ model | confirmPassword = v, error = Nothing }, Cmd.none)
        Submit ->
            if String.isEmpty model.name || String.isEmpty model.email || String.isEmpty model.password || String.isEmpty model.confirmPassword then
                ({ model | error = Just "Please fill in all fields." }, Cmd.none)
            else if not (String.contains "@" model.email) then
                ({ model | error = Just "Please enter a valid email address." }, Cmd.none)
            else if String.length model.password < 6 then
                ({ model | error = Just "Password must be at least 6 characters." }, Cmd.none)
            else if model.password /= model.confirmPassword then
                ({ model | error = Just "Passwords do not match." }, Cmd.none)
            else
                -- Simulate registration success
                ({ model | error = Nothing }, Cmd.none)
        ClearError -> ({ model | error = Nothing }, Cmd.none)

-- VIEW

view : Model -> Html Msg
view model =
    div [ class "auth-page" ]
        [ h1 [ style "text-align" "center", style "color" "#1976d2", style "margin-bottom" "32px" ] [ text "Register" ]
        , div [ class "card", style "max-width" "400px", style "margin" "0 auto", style "padding" "32px", style "box-shadow" "0 2px 12px rgba(0,0,0,0.07)", style "border-radius" "12px" ]
            [ form [ onSubmit Submit, style "display" "flex", style "flex-direction" "column", style "gap" "18px" ]
                [ div [ style "display" "flex", style "flex-direction" "column", style "gap" "6px" ]
                    [ label [ for "name", class "form-label", style "font-weight" "bold" ] [ text "Full Name" ]
                    , input [ id "name", name "name", type_ "text", value model.name, onInput UpdateName, placeholder "Enter your full name", required True, class "form-input", style "padding" "10px", style "border-radius" "4px", style "border" "1px solid #ccc" ] []
                    ]
                , div [ style "display" "flex", style "flex-direction" "column", style "gap" "6px" ]
                    [ label [ for "email", class "form-label", style "font-weight" "bold" ] [ text "Email" ]
                    , input [ id "email", name "email", type_ "email", value model.email, onInput UpdateEmail, placeholder "Enter your email", required True, class "form-input", style "padding" "10px", style "border-radius" "4px", style "border" "1px solid #ccc" ] []
                    ]
                , div [ style "display" "flex", style "flex-direction" "column", style "gap" "6px" ]
                    [ label [ for "password", class "form-label", style "font-weight" "bold" ] [ text "Password" ]
                    , input [ id "password", name "password", type_ "password", value model.password, onInput UpdatePassword, placeholder "Enter your password", required True, class "form-input", style "padding" "10px", style "border-radius" "4px", style "border" "1px solid #ccc" ] []
                    ]
                , div [ style "display" "flex", style "flex-direction" "column", style "gap" "6px" ]
                    [ label [ for "confirmPassword", class "form-label", style "font-weight" "bold" ] [ text "Confirm Password" ]
                    , input [ id "confirmPassword", name "confirmPassword", type_ "password", value model.confirmPassword, onInput UpdateConfirmPassword, placeholder "Confirm your password", required True, class "form-input", style "padding" "10px", style "border-radius" "4px", style "border" "1px solid #ccc" ] []
                    ]
                , button [ type_ "submit", class "btn-primary", style "width" "100%", style "margin-top" "8px", style "padding" "12px", style "font-size" "16px", style "border-radius" "4px" ] [ text "Register" ]
                ]
            , case model.error of
                Just err -> p [ style "color" "#d32f2f", style "margin-top" "16px", style "text-align" "center" ] [ text err ]
                Nothing -> text ""
            , div [ style "margin-top" "18px", style "text-align" "center" ]
                [ text "Already have an account? "
                , a [ href "/work/elm/login", style "color" "#1976d2", style "text-decoration" "underline" ] [ text "Login" ]
                ]
            ]
        ] 